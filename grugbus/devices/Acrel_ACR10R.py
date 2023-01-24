#!/usr/bin/python
# -*- coding: utf-8 -*-

#
#   Register data from datasheet PDF
#   Extracted from Acrel_ACR10R.csv
#

from grugbus.registers import *

def MakeRegisters():
    return (
    RegU16(   4,   0, 1, 'password'              , None, None   , 'int'  , 0, 'password'              , ''),
    RegU16(   4,   1, 1, 'addr_baud'             , None, None   , 'int'  , 0, 'addr_baud'             , ''),
    RegU16(   4,   3, 1, 'pt_transfo_ratio'      , None, None   , 'int'  , 0, 'pt_transfo_ratio'      , ''),
    RegU16(   4,   4, 1, 'ct_transfo_ratio'      , None, None   , 'int'  , 0, 'ct_transfo_ratio'      , ''),
    RegFloat( 4,  71, 1, 'import_active_energy'  ,    1, 'kWh'  , 'float', 4, 'Import Active Energy'  , ''),
    RegFloat( 4,  73, 1, 'export_active_energy'  ,    1, 'kWh'  , 'float', 4, 'Export Active Energy'  , ''),
    RegFloat( 4,  75, 1, 'import_reactive_energy',    1, 'kVArh', 'float', 4, 'Import Reactive Energy', ''),
    RegFloat( 4,  77, 1, 'export_reactive_energy',    1, 'kVArh', 'float', 4, 'Export Reactive Energy', ''),
    RegU16(   4,  97, 1, 'voltage'               ,  0.1, 'V'    , 'float', 1, 'Voltage'               , ''),
    RegU16(   4, 100, 1, 'current'               , 0.01, 'A'    , 'float', 2, 'Current'               , ''),
    RegS16(   4, 103, 1, 'active_power'          ,    1, 'W'    , 'float', 0, 'Active Power'          , ''),
    RegS16(   4, 107, 1, 'reactive_power'        ,    1, 'VAr'  , 'float', 0, 'Reactive Power'        , ''),
    RegS16(   4, 111, 1, 'apparent_power'        ,    1, 'VA'   , 'float', 0, 'Apparent Power'        , ''),
    RegU16(   4, 115, 1, 'power_factor'          , 0.01, ''     , 'float', 2, 'Power Factor'          , ''),
    RegFloat( 4, 119, 1, 'frequency'             , 0.01, 'Hz'   , 'float', 2, 'Frequency'             , ''))
if __name__ == "__main__":
    MakeRegisters()

