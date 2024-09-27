#!/usr/bin/python
# -*- coding: utf-8 -*-

from config_secret import *

##################################################################
# debugging tools
##################################################################

LOG_MODBUS_REQUEST_TIME = False
LOG_MODBUS_REQUEST_TIME_SDM630 = False
LOG_MODBUS_REQUEST_TIME_SDM120 = False
LOG_MODBUS_REQUEST_TIME_ABB    = False
LOG_MODBUS_WRITE_REQUEST_TIME  = False

LOG_MODBUS_REQUEST_TIME_SOLIS  = False

LOG_MODBUS_REGISTER_CHUNKS     = False

##################################################################
# mqtt
##################################################################

SOLARPI_IP			= "192.168.0.27"
MQTT_BROKER     	= SOLARPI_IP
MQTT_BROKER_LOCAL   = "127.0.0.1"

##################################################################
# Clickhouse
##################################################################

# Clickhouse
CLICKHOUSE_USER 	= "default"
CLICKHOUSE_PASSWORD = "shush"
CLICKHOUSE_INSERT_PERIOD_SECONDS = 5    # pool mqtt data and bulk insert into database every ... seconds

##################################################################
# MQTT Buffer
##################################################################

# MQTT Buffer server
MQTT_BUFFER_IP   = SOLARPI_IP
MQTT_BUFFER_PORT = 15555
MQTT_BUFFER_RETENTION = 24*3600*365 # how long to keep log files
MQTT_BUFFER_FILE_DURATION = 3600	# number of seconds before new log file is created

# path on solarpi for storage of mqtt compressed log
MQTT_BUFFER_PATH = "/home/peufeu/mqtt_buffer"
# temporary path on computer with clickhouse to copy logs and insert into database
MQTT_BUFFER_TEMP = "/mnt/ssd/temp/solarpi/mqtt"

##################################################################
# Modbus configuration
##################################################################
#
#   Use by-id so the ports don't move around after a plug and pray session
#   Note: FTDI FT_PROG utility can change serial number in FT2232 EEPROM, which
#   allows renaming serial ports.

COM_PORT_SOLIS1      = "/dev/serial/by-id/usb-FTDI_USB_RS485_1-if01-port0"   # Solis1 COM port
COM_PORT_FAKE_METER1 = "/dev/serial/by-id/usb-FTDI_USB_RS485_1-if00-port0"   # Solis1 fakemeter
COM_PORT_LOCALMETER1 = "/dev/serial/by-id/usb-FTDI_USB_RS485_4-if00-port0"   # Main meter

COM_PORT_SOLIS2      = "/dev/serial/by-id/usb-FTDI_USB_RS485_2-if01-port0"   # Solis1 COM port
COM_PORT_FAKE_METER2 = "/dev/serial/by-id/usb-FTDI_USB_RS485_2-if00-port0"   # Solis1 fakemeter
COM_PORT_LOCALMETER2 = "/dev/serial/by-id/usb-FTDI_USB_RS485_4-if01-port0"   # Main meter

COM_PORT_EVSE        = "/dev/serial/by-id/usb-FTDI_USB_RS485_3-if00-port0"   # Main meter
COM_PORT_METER       = "/dev/serial/by-id/usb-FTDI_USB_RS485_3-if01-port0"   # Main meter


# not used
# COM_PORT_METER       = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_54D2042261-if00"

# How often we modbus these devices
#   Tuples: (period, starting point in period)
#
POLL_PERIOD_METER       = 0.2
POLL_PERIOD_SOLIS_METER = 0.2
POLL_PERIOD_EVSE_METER  = 0.2
POLL_PERIOD_SOLIS       = 0.2
POLL_PERIOD_EVSE        = 1

# This overwrites some of the above parameters, like passwords.
# You have to create this file yourself.
from config_secret import *

##################################################################
# Measurement offset correction
##################################################################
CALIBRATION = {
    # Inverter internal current measurement offset
    # If measured value is exactly zero, keep it, otherwise add offset
    # "pv/solis2/battery_current" : (lambda x: x and (x+1.5))    ,
}

##################################################################
# Various config
##################################################################

# Along with other conditions on SOC, if battery current is zero during this time,
# we decide the inverter has finished charging it.
SOLIS_BATTERY_DCDC_DETECTION_TIME = 10

# Battery Full detection
SOLIS_BATTERY_FULL_SOC = 98

# Inverter auto turn on/off settings
SOLIS_TURNOFF_BATTERY_SOC  = 8
SOLIS_TURNOFF_MPPT_VOLTAGE = 50
SOLIS_TURNON_MPPT_VOLTAGE  = 80





############################################################################
# MQTT Rate Limit
#
#   topic: (period in seconds, absolute value change to force publish, mode)
#           0, 0    not allowed, period is mandatory to avoid flooding
#           60, 0   limit to every 60 seconds unless the value changes
#           60, 50  limit to every 60 seconds unless the value changes by 50
#
############################################################################

MQTT_RATE_LIMIT = {
    'pv/solis1/meter/active_power'                : (   1,     50.000, 'avg'   ), #         277/      13177
    'pv/meter/is_online'                          : (  60,      0.000, ''      ), #          46/      13175
    'pv/meter/total_power'                        : (   1,     10.000, 'avg'   ), #        3749/      13175
    'pv/total_pv_power'                           : (   1,     50.000, 'avg'   ), #         792/      13171
    'pv/total_battery_power'                      : (   1,     50.000, 'avg'   ), #        1209/      13171
    'pv/total_input_power'                        : (   1,     50.000, 'avg'   ), #         748/      13171
    'pv/battery_soc'                              : (  60,      0.000, 'avg'   ), #          48/      13171
    'pv/battery_max_charge_power'                 : (  60,     50.000, 'avg'   ), #          47/      13171
    'pv/total_grid_port_power'                    : (   1,     50.000, 'avg'   ), #          67/      13171
    'pv/solis1/fakemeter/active_power'            : (  10,     50.000, 'avg'   ), #          67/      13171
    'pv/meter/house_power'                        : (   1,     10.000, 'avg'   ), #        3441/      13171
    'pv/solis1/input_power'                       : (   1,    110.000, 'avg'   ), #         274/      13171
    'pv/router/excess_avg'                        : (   1,    110.000, 'avg'   ), #         273/      13145
    'pv/router/battery_min_charge_power'          : (  60,    150.000, 'avg'   ), #         265/      13145
    'pv/solis1/battery_power'                     : (  1,      25.000, 'avg'   ), #          59/       6771
    'pv/solis1/battery_voltage'                   : (  10,      0.200, 'avg'   ), #          46/       6771
    'pv/solis1/battery_current'                   : (  10,      0.500, 'avg'   ), #          58/       6771
    'pv/solis1/pv_power'                          : (  10,     50.000, 'avg'   ), #          49/       6771
    'pv/meter/phase_1_power'                      : (   1,     50.000, 'avg'   ), #        1323/       3294
    'pv/meter/phase_2_power'                      : (   1,     50.000, 'avg'   ), #        1322/       3294
    'pv/meter/phase_3_power'                      : (   1,     50.000, 'avg'   ), #        1324/       3294
    'pv/meter/phase_1_line_to_neutral_volts'      : (  10,      1.500, 'avg'   ), #          47/       3294
    'pv/meter/phase_2_line_to_neutral_volts'      : (  10,      1.500, 'avg'   ), #          48/       3294
    'pv/meter/phase_3_line_to_neutral_volts'      : (  10,      1.500, 'avg'   ), #          48/       3294
    'pv/meter/phase_1_current'                    : (  10,      0.100, 'avg'   ), #          56/       3294
    'pv/meter/phase_2_current'                    : (  10,      0.100, 'avg'   ), #          58/       3294
    'pv/meter/phase_3_current'                    : (  10,      0.100, 'avg'   ), #         398/       3294
    'pv/meter/total_export_kwh'                   : (  60,      0.010, 'avg'   ), #          46/       3294
    'pv/meter/total_import_kwh'                   : (  60,      0.010, 'avg'   ), #          46/       3294
    'pv/meter/total_power_factor'                 : (  10,      5.000, 'avg'   ), #          45/       3294
    'pv/meter/total_var'                          : (  10,    100.000, 'avg'   ), #          49/       3294
    'pv/meter/total_volt_amps'                    : (  10,    100.000, 'avg'   ), #          55/       3294
    'pv/meter/average_line_current_thd'           : (  60,    100.000, 'avg'   ), #         540/       3293
    'pv/meter/average_line_to_neutral_volts_thd'  : (  60,     10.000, 'avg'   ), #          45/       3293
    'pv/evse/charge_state'                        : (  60,      0.000, ''      ), #          46/       2635
    'pv/evse/current_limit'                       : (  60,      0.000, 'avg'   ), #          46/       2635
    'pv/evse/energy'                              : (  60,      0.010, ''      ), #          46/       2635
    'pv/evse/error_code'                          : (  60,      0.000, ''      ), #          45/       2635
    'pv/evse/socket_state'                        : (  60,      0.000, ''      ), #          46/       2635
    'pv/evse/meter/active_power'                  : (   1,     50.000, 'avg'   ), #         265/       1342
    'pv/solis1/meter/export_active_energy'        : (  60,      0.010, ''      ), #          46/       1317
    'pv/solis1/meter/import_active_energy'        : (  60,      0.010, ''      ), #          46/       1317
    'pv/evse/meter/voltage'                       : (  10,      1.500, 'avg'   ), #          47/       1208
    'pv/solis1/mppt1_voltage'                     : (   5,     10.000, 'avg'   ), #          48/        565
    'pv/solis1/mppt2_voltage'                     : (   5,     10.000, 'avg'   ), #          45/        565
    'pv/solis1/mppt1_current'                     : (   5,      0.100, 'avg'   ), #          73/        565
    'pv/solis1/mppt2_current'                     : (   5,      0.100, 'avg'   ), #          70/        565
    'pv/solis1/mppt1_power'                       : (   5,     50.000, 'avg'   ), #          46/        565
    'pv/solis1/mppt2_power'                       : (   5,     50.000, 'avg'   ), #          45/        565
    'pv/solis1/energy_generated_today'            : (  60,      0.010, ''      ), #          48/        565
    'pv/solis1/energy_generated_yesterday'        : (  60,      0.010, ''      ), #          46/        565
    'pv/solis1/backup_load_power'                 : (  60,     25.000, 'avg'   ), #          45/        564
    'pv/solis1/bms_battery_power'                 : (  10,     25.000, 'avg'   ), #          62/        564
    'pv/solis1/bms_battery_voltage'               : (  10,      0.200, 'avg'   ), #          46/        564
    'pv/solis1/dc_bus_half_voltage'               : (  10,     10.000, 'avg'   ), #          44/        564
    'pv/solis1/dc_bus_voltage'                    : (  10,     10.000, 'avg'   ), #          45/        564
    'pv/solis1/backup_voltage'                    : (  10,     10.000, 'avg'   ), #          45/        564
    'pv/solis1/phase_a_voltage'                   : (  10,     10.000, 'avg'   ), #          45/        564
    'pv/solis1/battery_max_charge_current'        : (  60,      0.500, 'avg'   ), #          45/        564
    'pv/solis1/battery_max_discharge_current'     : (  60,      0.500, 'avg'   ), #          45/        564
    'pv/solis1/bms_battery_charge_current_limit'  : (  60,      0.500, 'avg'   ), #          45/        564
    'pv/solis1/bms_battery_current'               : (  60,      0.500, 'avg'   ), #          62/        564
    'pv/solis1/bms_battery_discharge_current_limit': (  60,      0.500, 'avg'  ), #          45/        564
    'pv/solis1/battery_charge_energy_today'       : (  60,      0.010, ''      ), #          47/        564
    'pv/solis1/battery_discharge_energy_today'    : (  60,      0.010, ''      ), #          46/        564
    'pv/solis1/temperature'                       : (  10,      1.000, ''      ), #          46/        564
    'pv/solis1/bms_battery_health_soh'            : (  60,      0.000, ''      ), #          46/        564
    'pv/solis1/bms_battery_soc'                   : (  60,      0.000, ''      ), #          47/        564
    'pv/solis1/bms_battery_fault_information_01'  : (  60,      0.000, ''      ), #          45/        564
    'pv/solis1/bms_battery_fault_information_02'  : (  60,      0.000, ''      ), #          45/        564
    'pv/solis1/rwr_power_on_off'                  : (  60,      0.000, ''      ), #          46/        564
    'pv/solis1/fault_status_1_grid'               : (  60,      0.000, ''      ), #          45/        564
    'pv/solis1/fault_status_2_backup'             : (  60,      0.000, ''      ), #          45/        564
    'pv/solis1/fault_status_3_battery'            : (  60,      0.000, ''      ), #          45/        564
    'pv/solis1/fault_status_4_inverter'           : (  60,      0.000, ''      ), #          45/        564
    'pv/solis1/fault_status_5_inverter'           : (  60,      0.000, ''      ), #          45/        564
    'pv/solis1/inverter_status'                   : (  60,      0.000, ''      ), #          46/        564
    'pv/solis1/operating_status'                  : (  60,      0.000, ''      ), #          54/        564
    'pv/solis1/rwr_backup_output_enabled'         : (  60,      0.000, ''      ), #          46/        564
    'pv/solis1/rwr_energy_storage_mode'           : (  60,      0.000, ''      ), #          46/        564
    'cmnd/plugs/tasmota_t3/Power'                 : (  60,      0.000, ''      ), #          45/        552
    'pv/evse/rwr_current_limit'                   : (  60,      0.000, 'avg'   ), #          46/        264
    'pv/cpu_temp_c'                               : (  60,      1.000, ''      ), #          47/        264
    'pv/disk_space_gb'                            : (  60,      0.100, ''      ), #          46/        264
    'pv/cpu_load_percent'                         : (  60,      1.000, 'avg'   ), #         156/        263
    'pv/evse/meter/export_active_energy'          : ( 600,      0.010, ''      ), #           7/        134
    'pv/evse/meter/import_active_energy'          : (  60,      0.010, ''      ), #          46/        134
    'cmnd/plugs/tasmota_t4/Power'                 : (  60,      0.000, ''      ), #           3/          4
    'cmnd/plugs/tasmota_t2/Power'                 : (  60,      0.000, ''      ), #           3/          4
    'cmnd/plugs/tasmota_t1/Power'                 : (  60,      0.000, ''      ), #           3/          4
    'pv/evse/force_charge_until_kWh'              : (  60,      0.000, ''      ), #           2/          2
    'pv/evse/stop_charge_after_kWh'               : (  60,      0.000, ''      ), #           2/          2
    'pv/evse/start_excess_threshold_W'            : (  60,      0.000, ''      ), #           1/          1
    'pv/evse/charge_detect_threshold_W'           : (  60,      0.000, ''      ), #           1/          1
    'pv/evse/force_charge_minimum_A'              : (  60,      0.000, ''      ), #           1/          1
    'pv/evse/offset'                              : (  60,      0.000, ''      ), #           1/          1
    'pv/evse/settle_timeout_ms'                   : (  60,      0.000, ''      ), #           1/          1
    'pv/evse/command_interval_ms'                 : (  60,      0.000, ''      ), #           1/          1
    'pv/evse/command_interval_small_ms'           : (  60,      0.000, ''      ), #           1/          1
    'pv/evse/dead_band_W'                         : (  60,      0.000, ''      ), #           1/          1
    'pv/evse/stability_threshold_W'               : (  60,      0.000, ''      ), #           1/          1
    'pv/evse/small_current_step_A'                : (  60,      0.000, ''      ), #           1/          1
    'pv/evse/control_gain'                        : (  60,      0.000, ''      ), #           1/          1
    'pv/evse/charge_active'                       : (  60,      0.000, ''      ), #           0/          0
    'pv/evse/charging_unpaused'                   : (  60,      0.000, ''      ), #           0/          0
    'pv/evse/current'                             : (  60,      0.200, 'avg'   ), #           0/          0
    'pv/evse/req_time'                            : (  60,      0.010, 'avg'   ), #           0/          0
    'pv/evse/req_period'                          : (  60,      0.010, 'avg'   ), #           0/          0
    'pv/evse/meter/current'                       : (  60,      0.100, 'avg'   ), #           0/          0
}

for k,v in tuple(MQTT_RATE_LIMIT.items()):
    if k.startswith("pv/solis1/"):
        MQTT_RATE_LIMIT[k.replace("pv/solis1/","pv/solis2/")] = v


