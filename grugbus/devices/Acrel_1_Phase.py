#!/usr/bin/python
# -*- coding: utf-8 -*-

#
#   Register data from datasheet PDF
#   Extracted from Acrel_1_Phase.csv
#

from grugbus.registers import *

def MakeRegisters():
    return (
    RegU16( 4, 11, 1, 'voltage'             ,  0.1, 'V'  , 'float', 1, 'Voltage'             , ''),
    RegU16( 4, 12, 1, 'current'             , 0.01, 'A'  , 'float', 2, 'Current'             , ''),
    RegS16( 4, 13, 1, 'active_power'        ,    1, 'W'  , 'float', 0, 'Active Power'        , ''),
    RegS16( 4, 14, 1, 'reactive_power'      ,    1, 'VAr', 'float', 0, 'Reactive Power'      , ''),
    RegS16( 4, 15, 1, 'apparent_power'      ,    1, 'VA' , 'float', 0, 'Apparent Power'      , ''),
    RegS16( 4, 16, 1, 'power_factor'        , 0.01, ''   , 'float', 2, 'Power Factor'        , ''),
    RegU16( 4, 17, 1, 'frequency'           , 0.01, 'Hz' , 'float', 2, 'Frequency'           , ''),
    RegS32( 4, 60, 1, 'import_active_energy', 0.01, 'kWh', 'float', 2, 'Import Active Energy', ''),
    RegS32( 4, 62, 1, 'export_active_energy', 0.01, 'kWh', 'float', 2, 'Export Active Energy', ''))
if __name__ == "__main__":
    MakeRegisters()

