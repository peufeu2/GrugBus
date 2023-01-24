#!/usr/bin/python
# -*- coding: utf-8 -*-

import os, pprint, time, sys, re
from path import Path
import csv

"""
    How to use this stuff

    1) Open one of the existing CSVs and look at it

    2) Make a CSV that looks like it by copypasting datasheet register tables into spreadsheet
    Note: copying from PDF will usually keep the table format, which helps immensely.

    3) Run this program on it, it should spit errors, then after errors are corrected, some python code

    4) Edit python code if necessary

    5) Put python code file with the other similar ones and import it.

Input CSV columns:

function        Modbus function code: can be 1 2 3 4, write codes will be added
addr            Register address as seen on the bus (no "register number but then you add offset" bullshit")
addr_end        Last word of the register if it's an array, otherwise ""
type            U16 S16 U32 S32 Bool
unit            something like "0.01kWh" if a value of 1 in the register represents 0.01 kWh. It will be parsed into number and unit.
name            human readable name like "output power"
name_for_id     machine readable id like "output_power", leave blank to auto-generate, but if name contains heretical characters it won't work, so you can enter it manually. note id must be unique
description     self-explanatory

Output Python code:

See output file.

"""

def run( regfile ):
    unit_transformations = {}
    register_ids = {}
    all_regs = []

    with open(regfile+".csv", newline='') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            # cleanup
            row = { k:v.strip() for k,v in row.items() }
            print(row)
            try:
                if not row["addr"]:
                    continue

                # merge multiple spaces
                for l in "name","description","name_for_id":
                    row["name"] = re.sub( " +", " ", row["name"] )

                # format unit
                u = row["unit"].replace(",",".").replace("Var","VAr")
                if u:
                    m = re.match( r"^([\-0-9\..]+)\s*(.*)$", u )
                    if not m:
                        value = 1
                        unit = u
                    else:
                        value, unit = m.groups()
                    unit_transformations[ u ] = value, unit
                    row["unit"] = unit
                    row["unit_value"] = float(value)
                    intv = int(row["unit_value"])
                    if row["unit_value"] == intv:
                        row["unit_value"] = intv
                else:
                    row["unit"] = None
                    row["unit_value"] = None

                # use proper types
                for k in "function", "addr", "addr_end", "length":
                    row.setdefault(k,None)
                    if row[k]:  row[k] = int(row[k],0)
                    else:       row[k] = None

                row["type"] = row["type"].capitalize()

                pprint.pprint( row )

                assert row["type"] in ("Bool","U16","S16","U32","S32","Float")
                assert row["name"]
                assert row["function"]

                if row["type"] in ("Bool","U16","S16"):
                    if row["addr_end"]:
                        row["length"] = row["addr_end"]-row["addr"]+1
                    elif not row["length"]:
                        row["length"] = 1
                elif row["type"] in ("U32","S32","Float"):
                    if row["addr_end"]:
                        l = row["addr_end"]-row["addr"]+1
                    elif not row["length"]:
                        l = 2
                    else:
                        l = row["length"]
                    # l is length in words, let's compute length in number of 32-bit values instead
                    row["length"] = l//2
                    if row["length"]*2 != l:
                        pprint.pprint( row )
                        input("Check addr_end and length, looks like half of a 32-bit value is missing?")
                
                del row["addr_end"]
                if not row["length"]:
                    pprint.pprint( row )
                    input("missing addr_end or length?")

                if row["name"].lower().startswith( "reserved" ):
                    row["name"] += "_%d" % row["addr"]
                else:
                    if row["type"] in ("Bool","U16","S16"):
                        if row["length"] > 1:
                            pprint.pprint( row )
                            input("Check addr_end?")

                print( row )
                ut = row["usertype"].lower()
                assert ut in ("int","float","bool","bitfield")
                row["usertype"] = ut

                if row["decimals"] not in (None,""):
                    row["decimals"] = int(row["decimals"])
                else:
                    row["decimals"] = None

                # It's important to distinguish between read/write and read only registers,
                # impossible to remember it just from the name, unless... Grugarian notation!
                # Type (U16/S32 etc) is ignored, what matters is to know if we can write to it
                # or not just by looking at the name.
                f = row["function"]
                if f==1:
                    row["function"] = 1,5,15
                    prefix = "rwb_"       # read write bit
                if f==2:
                    prefix = "b_"         # read only bit
                elif f==3:
                    row["function"] = 3,6,16
                    prefix = "rwr_"       # read write register
                elif f==4:
                    prefix = ""           # read only register

                row["id"] = prefix+(row["name_for_id"] or row["name"]).lower().replace(" ","_")

                assert re.match( r"^[0-9_a-z]+$", row["id"] )
            finally:
                print(row)

            # check name unicity
            register_ids.setdefault( row["id"], [] ).append( row )

            all_regs.append( row )
            print(row)

    print( "Units modified, for verification:" )
    pprint.pprint( unit_transformations )

    print( "Duplicate ids:" )
    dupes = 0
    for reg_id, regs in register_ids.items():
        if len(regs) == 1: continue
        dupes += 1
        print( "reg_id: <%s>" % reg_id )
        for reg in regs:
            print( "    ", reg )
    if( dupes ):
        print("Can't continue with duplicate ids!")
        sys.exit(1)

    print( "Duplicate addresses:" )
    dupes = 0
    regs_by_func_addr = {}
    for reg in all_regs:
        func = reg["function"]
        for addr in range( reg["addr"], reg["addr"]+reg["length"] ):
            if (func, addr) in regs_by_func_addr:
                print("Duplicate registers at fcode %d address %d:" % (fcode,addr) )
                print( regs_by_func_addr[addr] )
                print( reg )
                dupes += 1
            regs_by_func_addr[(func,addr)] = reg
    if( dupes ):
        input("Duplicate addresses are allowed (for example date available as timestamp and Y,M,D registers, but it's not supported. Continue?" )
        sys.exit(1)

    def key(x):
        if type(x["function"]) == tuple:
            return x["function"][0],x["addr"]
        return x["function"],x["addr"]

    all_regs = sorted( all_regs, key=key )

    # generate python code
    cols = (
             ( 'type'          , "left"     ),
             ( 'function'      , "right"    ), 
             ( 'addr'          , "right"    ), 
             ( 'length'        , "right"    ), 
             ( 'id'            , "left"     ),
             ( 'unit_value'    , "right"    ), 
             ( 'unit'          , "left"     ), 
             ( "usertype"      , "left"     ),
             ( "decimals"      , "left"     ),
             ( 'name'          , "left"     ), 
             ( 'description'   , "left"     ) 
            )

    col_widths = { col:0 for col,_ in cols }
    for reg in all_regs:
        for col,_ in cols:
            if col == "type":
                v = reg[col]
            else:
                v = repr(reg[col])
            reg[col] = v
            col_widths[col] = max( col_widths[col], len(v) )

    lines = []
    for reg in all_regs:
        print( repr(reg["function"]),repr(reg["addr"]) )
        c = []
        header = "    Reg%s( "%reg["type"]
        for col,justification in cols:
            spaces = " "*(col_widths[col]-len(reg[col]))
            if col == "type":
                header += spaces
            else:
                if justification == "right":
                    c.append( spaces+reg[col] )
                else:
                    c.append( reg[col]+spaces )
        lines.append( header + ", ".join( c ) + ")" )

    outf = open(regfile+".py","w",encoding="utf-8")
    outf.write( """#!/usr/bin/python
# -*- coding: utf-8 -*-

#
#   Register data from datasheet PDF
#   Extracted from %s.csv
#

from grugbus.registers import *

def MakeRegisters():
    return (
""" % regfile )

    outf.write( ",\n".join(lines) )

    outf.write( """)
if __name__ == "__main__":
    MakeRegisters()

""" )


for regfile in "Eastron_SDM120", "Acrel_ACR10RH", "Eastron_SDM630", "Solis_S5_EH1P_6K_2020", "Acrel_ACR10R", "Acrel_1_Phase":
    run(regfile)
