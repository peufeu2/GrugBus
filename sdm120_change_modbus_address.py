#!/usr/bin/python
# -*- coding: utf-8 -*-

import os, pprint, time, sys, serial, socket, traceback, struct, datetime, logging, math, traceback, shutil, collections
from path import Path

# This program is supposed to run on a potato (Allwinner H3 SoC) and uses async/await,
# so import the fast async library uvloop
import asyncio

# Modbus
import pymodbus
from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient
from pymodbus.pdu import ExceptionResponse

# Device wrappers and misc local libraries
from misc import *
import grugbus
from grugbus.devices import Eastron_SDM120, Fronius_TS100A
import config

# pymodbus.pymodbus_apply_logging_config( logging.DEBUG )
logging.basicConfig( encoding='utf-8', 
                     level=logging.INFO,
                     format='[%(asctime)s] %(levelname)s:%(message)s',
                     handlers=[logging.FileHandler(filename=Path(__file__).stem+'.log'), 
                            logging.StreamHandler(stream=sys.stdout)])
log = logging.getLogger(__name__)

# set max reconnect wait time for Fronius
# pymodbus.constants.Defaults.ReconnectDelayMax = 60000   # in milliseconds


#
#   Helper to set modbus address of a SDM120 smartmeter during installation:
#       
#       It has default address 1, so use this function to change it.
#


async def runme():

    m = AsyncModbusSerialClient(
                    port            = "COM7",
                    timeout         = 1,
                    retries         = 2,
                    baudrate        = 9600,
                    bytesize        = 8,
                    parity          = "N",
                    stopbits        = 1,
                )
    print( m )
    modbus_address = 1
    await m.connect()
    addr = 258
    resp = await m.read_holding_registers( addr, 16, modbus_address )
    print( addr, resp )
    print( addr, "read", resp.registers )

    addr = 1024
    resp = await m.read_holding_registers( addr, 16, modbus_address )
    print( addr, resp )
    print( addr, "read", resp.registers )
    # for addr in range( 0,1000, 10 ):
        # resp = await self.modbus.read_input_registers( addr, 1, 1 )

# asyncio.run( runme() )
# stop

if 1:
    async def set_sdm120_address( new_address=1 ):
        modbus_address = 1
        d = grugbus.SlaveDevice( 
                AsyncModbusSerialClient(
                    port            = "COM7",
                    timeout         = 1,
                    retries         = 2,
                    baudrate        = 9600,
                    bytesize        = 8,
                    parity          = "N",
                    stopbits        = 1,
                ),
                modbus_address,          # Modbus address
                "meter", "SDM120 Smartmeter", 
                Fronius_TS100A.MakeRegisters() )
        await d.modbus.connect()

        addr = 258
        resp = await d.modbus.read_holding_registers( addr, 16, modbus_address )
        print( addr, resp )
        print( addr, "read", resp.registers )

        d.rwr_import_active_energy_kwh.write( 0 )

        # while True:
            # regs = await d.read_regs( [ d.rwr_active_power ] )
            # for reg in regs:
                # print( "%40s %s" % (reg.key, reg.format_value()))
            # await asyncio.sleep(0.1)


        while True:
            regs = await d.read_regs( d.registers )
            for reg in regs:
                print( "%40s %s" % (reg.key, reg.format_value()))
            await asyncio.sleep(1)

if 0:
    async def set_sdm120_address( new_address=1 ):
        d = grugbus.SlaveDevice( 
                AsyncModbusSerialClient(
                    port            = config.COM_PORT_LOCALMETER1,
                    timeout         = 1,
                    retries         = 2,
                    baudrate        = 9600,
                    bytesize        = 8,
                    parity          = "N",
                    stopbits        = 1,
                ),
                3,          # Modbus address
                "meter", "SDM120 Smartmeter", 
                Eastron_SDM120.MakeRegisters() )
        await d.modbus.connect()

        # SDM120 does not like "write register", it needs "write multiple registers" even if there is just one
        d.force_multiple_regiters = True

        print("Checking meter on address %s" % d.bus_address )
        await d.read_regs( d.registers )
        for reg in d.registers:
            print( reg.key, reg.value )

        print()
        print( "current address", d.rwr_modbus_node_address.value )

        while True:
            await asyncio.sleep(1)
            try:
                await d.rwr_measurement_mode.read()
                print( "measurement_mode", d.rwr_measurement_mode.value )
            except Exception as e:
                print(e)


        # Display Import - Export (default is Import + Export)
        # await d.rwr_measurement_mode.write( 3 )
        await d.rwr_measurement_mode.read()
        print( "measurement_mode", d.rwr_measurement_mode.value )

        # # set scroll time to 0 (no scrolling)
        # await d.rwr_display_config.write( 0 )
        await d.rwr_display_config.read()
        print( "display_config : 0x%04x" % d.rwr_display_config.value )


        # input( "Long press button on meter until display --SET-- to enable modbus writes (otherwise it is protected) and press ENTER" )
        # d.rwr_modbus_node_address.value = new_address
        # await d.rwr_modbus_node_address.write()

        # print( "Write done, if it was successful the meter should not respond to the old address and this should raise a Timeout...")

        # await d.rwr_modbus_node_address.read()
        # print( "current address", d.rwr_modbus_node_address.value )


asyncio.run( set_sdm120_address() )
