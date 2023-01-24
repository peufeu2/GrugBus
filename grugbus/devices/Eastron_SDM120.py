#!/usr/bin/python
# -*- coding: utf-8 -*-

#
#   Register data from datasheet PDF
#   Extracted from Eastron_SDM120.csv
#

from grugbus.registers import *

def MakeRegisters():
    return (
    RegFloat( (3, 6, 16),    10, 1, 'rwr_relay_pulse_width'                  , None, None   , 'float', None, 'Relay Pulse Width'                  , 'Write relay on period in milliseconds: \n60, 100 or 200, default 200.'                                                                                                                                                                                           ),
    RegFloat( (3, 6, 16),    18, 1, 'rwr_modbus_parity_stop'                 , None, None   , 'float', None, 'Modbus Parity Stop'                 , 'Write the Modbus port parity/stop bits for MODBUS Protocol, where: \n0 = One stop bit and no parity, default. \n1 = One stop bit and even parity. \n2 = One stop bit and odd parity.\n3 = Two stop bits and no parity. \nRequires a restart to become effective.'),
    RegFloat( (3, 6, 16),    20, 1, 'rwr_modbus_node_address'                , None, None   , 'float', None, 'Modbus Node Address'                , 'Write the Modbus port node address: \n1 to 247 for MODBUS Protocol, default 1. \nRequires a restart to become effective. \nNote, both the MODBUS Protocol and Johnson Controls node addresses \nCan be changed via the display setup menus.'                     ),
    RegFloat( (3, 6, 16),    28, 1, 'rwr_modbus_baud_rate'                   , None, None   , 'float', None, 'Modbus Baud Rate'                   , 'Write the Modbus port baud rate for MODBUS Protocol, where:\n0 = 2400 baud. 1 = 4800 baud.2 = 9600 baud, default.3 = 19200 baud. 4 = 38400 baud. \nRequires a restart to becomeeffective'                                                                        ),
    RegFloat( (3, 6, 16), 62720, 1, 'rwr_display_config'                     , None, None   , 'float', None, 'Display Config'                     , 'Demand Interval, Slide Time, Automatic Scroll Display Interval (Scroll Time), Backlight Time\nData Format: BCDmin-min-s-min\nScroll Time=0: the display does not scroll automatically \nBacklight Time=0: Backlight is Always On.'                               ),
    RegFloat( (3, 6, 16), 63760, 1, 'rwr_display_format'                     , None, None   , 'float', None, 'Display Format'                     , 'Default Format: Hex\n0000: 0.001kWh (kVArh) (default)\n0001: 0.01kWh (kVArh)\n0002: 0.1kWh (kVArh)\n0003: 1kWh (kVArh)'                                                                                                                                          ),
    RegU16(   (3, 6, 16), 63776, 1, 'rwr_measurement_mode'                   , None, None   , 'int'  , None, 'Measurement Mode'                   , 'Data Format: Hex\n0001: Mode 1 (Total = Import)\n0002: Mode 2 (Total = Import + Export)\n0003: Mode 3 (Total = Import - Export)'                                                                                                                                 ),
    RegFloat( (3, 6, 16), 63792, 1, 'rwr_pulse_output_and_led_indicator_mode', None, None   , 'float', None, 'Pulse Output and LED Indicator Mode', 'Data Format: Hex\n0000: Import & Export Energy, LED flashes for Import & Export Energy\n0001: Import Energy, LED flashes for Import Energy only\n0002: Export Energy, LED flashes for Export Energy only'                                                        ),
    RegFloat(          4,     0, 1, 'voltage'                                ,    1, 'V'    , 'float', 0   , 'Voltage'                            , ''                                                                                                                                                                                                                                                                ),
    RegFloat(          4,     6, 1, 'current'                                ,    1, 'A'    , 'float', 3   , 'Current'                            , ''                                                                                                                                                                                                                                                                ),
    RegFloat(          4,    12, 1, 'active_power'                           ,    1, 'W'    , 'float', 0   , 'Active Power'                       , ''                                                                                                                                                                                                                                                                ),
    RegFloat(          4,    18, 1, 'apparent_power'                         ,    1, 'VA'   , 'float', 0   , 'Apparent Power'                     , ''                                                                                                                                                                                                                                                                ),
    RegFloat(          4,    24, 1, 'reactive_power'                         ,    1, 'VAr'  , 'float', 0   , 'Reactive Power'                     , ''                                                                                                                                                                                                                                                                ),
    RegFloat(          4,    30, 1, 'power_factor'                           , None, None   , 'float', 3   , 'Power Factor'                       , ''                                                                                                                                                                                                                                                                ),
    RegFloat(          4,    36, 1, 'phase_angle'                            ,    1, '°'    , 'float', 3   , 'Phase Angle'                        , ''                                                                                                                                                                                                                                                                ),
    RegFloat(          4,    70, 1, 'frequency'                              ,    1, 'Hz'   , 'float', 3   , 'Frequency'                          , ''                                                                                                                                                                                                                                                                ),
    RegFloat(          4,    72, 1, 'import_active_energy'                   ,    1, 'kWh'  , 'float', 3   , 'Import Active Energy'               , ''                                                                                                                                                                                                                                                                ),
    RegFloat(          4,    74, 1, 'export_active_energy'                   ,    1, 'kWh'  , 'float', 3   , 'Export Active Energy'               , ''                                                                                                                                                                                                                                                                ),
    RegFloat(          4,    76, 1, 'import_reactive_energy'                 ,    1, 'kVArh', 'float', 3   , 'Import Reactive Energy'             , ''                                                                                                                                                                                                                                                                ),
    RegFloat(          4,    78, 1, 'export_reactive_energy'                 ,    1, 'kVArh', 'float', 3   , 'Export Reactive Energy'             , ''                                                                                                                                                                                                                                                                ),
    RegFloat(          4,   342, 1, 'total_active_energy'                    ,    1, 'kWh'  , 'float', 3   , 'Total Active Energy'                , ''                                                                                                                                                                                                                                                                ),
    RegFloat(          4,   344, 1, 'total_reactive_energy'                  ,    1, 'kVArh', 'float', 3   , 'Total Reactive Energy'              , ''                                                                                                                                                                                                                                                                ))
if __name__ == "__main__":
    MakeRegisters()

