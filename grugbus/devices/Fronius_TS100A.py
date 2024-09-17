#!/usr/bin/python
# -*- coding: utf-8 -*-

#
#   Register data from datasheet PDF
#   Extracted from Fronius TS100A.csv
#

from grugbus.registers import *

def MakeRegisters():
    return (
    RegS32( (3, 6, 16),  258, 1, 'rwr_voltage'                     ,   0.1, 'V'    , 'float', 1, 'Voltage'                     , '', swap_words=True ),
    RegS32( (3, 6, 16),  262, 1, 'rwr_active_power'                ,   0.1, 'W'    , 'float', 1, 'Active Power'                , '', swap_words=True ),
    RegS32( (3, 6, 16),  264, 1, 'rwr_apparent_power'              ,   0.1, 'VA'   , 'float', 1, 'Apparent Power'              , '', swap_words=True ),
    RegS32( (3, 6, 16),  266, 1, 'rwr_reactive_power'              ,   0.1, 'VAr'  , 'float', 1, 'Reactive Power'              , '', swap_words=True ),
    RegS32( (3, 6, 16),  268, 1, 'rwr_power_factor'                , 0.001, '°'    , 'float', 3, 'Power Factor'                , '', swap_words=True ),
    RegS32( (3, 6, 16),  272, 1, 'rwr_frequency'                   ,   0.1, 'Hz'   , 'float', 1, 'Frequency'                   , '', swap_words=True ),
    RegS32( (3, 6, 16), 1024, 1, 'rwr_import_active_energy_kwh'    ,     1, 'kWh'  , 'float', 0, 'Import Active Energy kWh'    , '', swap_words=True ),
    RegS32( (3, 6, 16), 1026, 1, 'rwr_import_active_energy_wh'     ,     1, 'Wh'   , 'float', 0, 'Import Active Energy Wh'     , '', swap_words=True ),
    RegS32( (3, 6, 16), 1028, 1, 'rwr_import_reactive_energy_kvarh',     1, 'kVArh', 'float', 0, 'Import Reactive Energy kVArh', '', swap_words=True ),
    RegS32( (3, 6, 16), 1030, 1, 'rwr_import_reactive_energy_varh' ,     1, 'VArh' , 'float', 0, 'Import Reactive Energy Varh' , '', swap_words=True ),
    RegS32( (3, 6, 16), 1032, 1, 'rwr_export_active_energy_kwh'    ,     1, 'kWh'  , 'float', 0, 'Export Active Energy kWh'    , '', swap_words=True ),
    RegS32( (3, 6, 16), 1034, 1, 'rwr_export_active_energy_wh'     ,     1, 'Wh'   , 'float', 0, 'Export Active Energy Wh'     , '', swap_words=True ),
    RegS32( (3, 6, 16), 1036, 1, 'rwr_export_reactive_energy_kvarh',     1, 'kVArh', 'float', 0, 'Export Reactive Energy kVArh', '', swap_words=True ),
    RegS32( (3, 6, 16), 1038, 1, 'rwr_export_reactive_energy_varh' ,     1, 'VArh' , 'float', 0, 'Export Reactive Energy Varh' , '', swap_words=True ))
if __name__ == "__main__":
    MakeRegisters()

