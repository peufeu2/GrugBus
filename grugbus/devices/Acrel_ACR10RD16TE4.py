#!/usr/bin/python
# -*- coding: utf-8 -*-

#
#   Register data from datasheet PDF
#   Extracted from Acrel_ACR10RD16TE4.csv
#

from grugbus.registers import *

def MakeRegisters():
    return (
    RegU16( 4, 243, 1, 'voltage'                      ,  0.1, 'V'  , 'float', 1, 'Phase 1 line to neutral volts', ''),
    RegU16( 4, 244, 1, 'phase_2_line_to_neutral_volts',  0.1, 'V'  , 'float', 1, 'Phase 2 line to neutral volts', ''),
    RegU16( 4, 245, 1, 'phase_3_line_to_neutral_volts',  0.1, 'V'  , 'float', 1, 'Phase 3 line to neutral volts', ''),
    RegU16( 4, 246, 1, 'phase_1_to_2_volts'           ,  0.1, 'V'  , 'float', 1, 'Phase 1 to 2 volts'           , ''),
    RegU16( 4, 247, 1, 'phase_2_to_3_volts'           ,  0.1, 'V'  , 'float', 1, 'Phase 2 to 3 volts'           , ''),
    RegU16( 4, 248, 1, 'phase_3_to_1_volts'           ,  0.1, 'V'  , 'float', 1, 'Phase 3 to 1 volts'           , ''),
    RegU16( 4, 249, 1, 'current'                      ,  0.1, 'A'  , 'float', 3, 'Phase 1 current'              , ''),
    RegU16( 4, 250, 1, 'phase_2_current'              ,  0.1, 'A'  , 'float', 3, 'Phase 2 current'              , ''),
    RegU16( 4, 251, 1, 'phase_3_current'              ,  0.1, 'A'  , 'float', 3, 'Phase 3 current'              , ''),
    RegU16( 4, 252, 1, 'frequency'                    , 0.01, 'Hz' , 'float', 2, 'Frequency'                    , ''),
    RegS32( 4, 253, 1, 'phase_1_active_power'         , 1.13, 'W'  , 'float', 2, 'Phase 1 active power'         , ''),
    RegS32( 4, 255, 1, 'phase_2_active_power'         , 1.13, 'W'  , 'float', 2, 'Phase 2 active power'         , ''),
    RegS32( 4, 257, 1, 'phase_3_active_power'         , 1.13, 'W'  , 'float', 2, 'Phase 3 active power'         , ''),
    RegS32( 4, 259, 1, 'active_power'                 , 1.13, 'W'  , 'float', 2, 'Active Power'                 , ''),
    RegS32( 4, 261, 1, 'phase_1_reactive_power'       ,    1, 'VAr', 'float', 2, 'Phase 1 reactive power'       , ''),
    RegS32( 4, 263, 1, 'phase_2_reactive_power'       ,    1, 'VAr', 'float', 2, 'Phase 2 reactive power'       , ''),
    RegS32( 4, 265, 1, 'phase_3_reactive_power'       ,    1, 'VAr', 'float', 2, 'Phase 3 reactive power'       , ''),
    RegS32( 4, 267, 1, 'reactive_power'               ,    1, 'VAr', 'float', 2, 'Reactive Power'               , ''),
    RegS32( 4, 269, 1, 'phase_1_volt_amps'            ,    1, 'VA' , 'float', 2, 'Phase 1 volt amps'            , ''),
    RegS32( 4, 271, 1, 'phase_2_volt_amps'            ,    1, 'VA' , 'float', 2, 'Phase 2 volt amps'            , ''),
    RegS32( 4, 273, 1, 'phase_3_volt_amps'            ,    1, 'VA' , 'float', 2, 'Phase 3 volt amps'            , ''),
    RegS32( 4, 275, 1, 'apparent_power'               ,    1, 'VA' , 'float', 2, 'Apparent power'               , ''),
    RegS16( 4, 277, 1, 'phase_1_power_factor'         ,  0.1, ''   , 'float', 3, 'Phase 1 power factor'         , ''),
    RegS16( 4, 278, 1, 'phase_2_power_factor'         ,  0.1, ''   , 'float', 3, 'Phase 2 power factor'         , ''),
    RegS16( 4, 279, 1, 'phase_3_power_factor'         ,  0.1, ''   , 'float', 3, 'Phase 3 power factor'         , ''),
    RegS16( 4, 280, 1, 'power_factor'                 ,  0.1, ''   , 'float', 3, 'Power Factor'                 , ''),
    RegS32( 4, 365, 1, 'import_active_energy'         ,    1, 'kWh', 'float', 2, 'Import Active Energy'         , ''),
    RegS32( 4, 367, 1, 'export_active_energy'         ,    1, 'kWh', 'float', 2, 'Export Active Energy'         , ''))
if __name__ == "__main__":
    MakeRegisters()

