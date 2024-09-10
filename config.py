#!/usr/bin/python
# -*- coding: utf-8 -*-

from config_secret import *

##################################################################
# debugging tools
##################################################################

LOG_MODBUS_REQUEST_TIME = False
LOG_MODBUS_WRITE_REQUEST_TIME = True

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
POLL_PERIOD_METER       = (0.2, 0.1)
POLL_PERIOD_SOLIS_METER = (0.2, 0.0)
POLL_PERIOD_EVSE_METER  = (0.2, 0.0)
POLL_PERIOD_SOLIS       = (0.2, 0.0)
POLL_PERIOD_EVSE        = (1, 0.5)

# Inverter auto turn on/off settings
SOLIS_TURNOFF_BATTERY_SOC  = 10
SOLIS_TURNOFF_MPPT_VOLTAGE = 30
SOLIS_TURNON_MPPT_VOLTAGE  = 80

# This overwrites some of the above parameters, like passwords.
# You have to create this file yourself.
from config_secret import *

############################################################################
# MQTT Rate Limit
#
#   topic: (period in seconds, absolute value change to force publish)
#           0, 0    not allowed, period is mandatory to avoid flooding
#           60, 0   limit to every 60 seconds unless the value changes
#           60, 50  limit to every 60 seconds unless the value changes by 50
#
############################################################################
MQTT_RATE_LIMIT = {
    # Frequent
    "pv/total_pv_power"                              : ( 1,  15 ),
    "pv/meter/total_power"                           : ( 1,  10 ),
    "pv/meter/house_power"                           : ( 1,  10 ),
    "pv/solis1/input_power"                          : ( 1,  10 ),
    "pv/evse/meter/active_power"                     : ( 60, 10 ),
    "pv/router/excess_avg"                           : ( 1,  10 ),
    "pv/solis1/meter/active_power"                   : ( 1,  10 ),

    # EVSE
    "pv/evse/charge_state"                           : ( 60, 0 ),
    "pv/evse/charging_unpaused"                      : ( 60, 0 ),
    "pv/evse/current"                                : ( 60, 0.2 ),
    "pv/evse/current_limit"                          : ( 60, 0 ),
    "pv/evse/energy"                                 : ( 60, 0.01 ),
    "pv/evse/error_code"                             : ( 60, 0 ),
    "pv/evse/req_time"                               : ( 60, 0.01 ),
    "pv/evse/rwr_current_limit"                      : ( 60, 0 ),
    "pv/evse/socket_state"                           : ( 60, 0 ),
    "pv/evse/virtual_current_limit"                  : ( 60, 0 ),

    "pv/evse/meter/export_active_energy"             : ( 60, 0.01 ),
    "pv/evse/meter/import_active_energy"             : ( 60, 0.01 ),
    "pv/evse/meter/voltage"                          : ( 10, 1 ),
    "pv/evse/meter/current"                          : ( 10, 0.1 ),

    # Meter
    "pv/meter/phase_1_power"                         : ( 10, 25 ),
    "pv/meter/phase_2_power"                         : ( 10, 25 ),
    "pv/meter/phase_3_power"                         : ( 10, 25 ),
    "pv/meter/average_line_current_thd"              : ( 10, 10 ),
    "pv/meter/average_line_to_neutral_volts_thd"     : ( 10, 10 ),
    "pv/meter/is_online"                             : ( 10, 0 ),
    "pv/meter/phase_1_line_to_neutral_volts"         : ( 10, 1.5 ),
    "pv/meter/phase_2_line_to_neutral_volts"         : ( 10, 1.5 ),
    "pv/meter/phase_3_line_to_neutral_volts"         : ( 10, 1.5 ),
    "pv/meter/phase_1_current"                       : ( 10, 0.1 ),
    "pv/meter/phase_2_current"                       : ( 10, 0.1 ),
    "pv/meter/phase_3_current"                       : ( 10, 0.1 ),
    "pv/meter/total_export_kwh"                      : ( 10, 0.01 ),
    "pv/meter/total_import_kwh"                      : ( 10, 0.01 ),
    "pv/meter/total_power_factor"                    : ( 10, 2 ),
    "pv/meter/total_var"                             : ( 10, 25 ),
    "pv/meter/total_volt_amps"                       : ( 10, 25 ),

    # Solis1
    "pv/solis1/backup_load_power"                    : ( 10, 25 ),
    "pv/solis1/battery_power"                        : ( 10, 25 ),
    "pv/solis1/bms_battery_power"                    : ( 10, 25 ),

    "pv/solis1/battery_voltage"                      : ( 10, 0.1 ),
    "pv/solis1/bms_battery_voltage"                  : ( 10, 0.1 ),
    "pv/solis1/dc_bus_half_voltage"                  : ( 10, 3 ),
    "pv/solis1/dc_bus_voltage"                       : ( 10, 3 ),
    "pv/solis1/backup_voltage"                       : ( 10, 3 ),
    "pv/solis1/mppt1_voltage"                        : ( 10, 3 ),
    "pv/solis1/mppt2_voltage"                        : ( 10, 3 ),
    "pv/solis1/phase_a_voltage"                      : ( 10, 3 ),

    "pv/solis1/battery_current"                      : ( 10, 0.1 ),
    "pv/solis1/battery_max_charge_current"           : ( 10, 0.1 ),
    "pv/solis1/battery_max_discharge_current"        : ( 10, 0.1 ),
    "pv/solis1/bms_battery_charge_current_limit"     : ( 10, 0.1 ),
    "pv/solis1/bms_battery_current"                  : ( 10, 0.1 ),
    "pv/solis1/bms_battery_discharge_current_limit"  : ( 10, 0.1 ),

    "pv/solis1/mppt1_current"                        : ( 10, 0.1 ),
    "pv/solis1/mppt2_current"                        : ( 10, 0.1 ),

    "pv/solis1/mppt1_power"                          : ( 10, 25 ),
    "pv/solis1/mppt2_power"                          : ( 10, 25 ),
    "pv/solis1/pv_power"                             : ( 10, 25 ),

    "pv/solis1/battery_charge_energy_today"          : ( 60, 0.01 ),
    "pv/solis1/battery_discharge_energy_today"       : ( 60, 0.01 ),
    "pv/solis1/energy_generated_today"               : ( 60, 0.01 ),
    "pv/solis1/energy_generated_yesterday"           : ( 60, 0.01 ),

    "pv/solis1/temperature"                          : ( 60, 1 ),
    "pv/solis1/bms_battery_health_soh"               : ( 60, 0 ),
    "pv/solis1/bms_battery_soc"                      : ( 60, 0 ),

    "pv/solis1/bms_battery_fault_information_01"     : ( 60, 0 ),
    "pv/solis1/bms_battery_fault_information_02"     : ( 60, 0 ),

    "pv/solis1/rwr_power_on_off"                     : ( 60, 0 ),
    "pv/solis1/fault_status_1_grid"                  : ( 60, 0 ),
    "pv/solis1/fault_status_2_backup"                : ( 60, 0 ),
    "pv/solis1/fault_status_3_battery"               : ( 60, 0 ),
    "pv/solis1/fault_status_4_inverter"              : ( 60, 0 ),
    "pv/solis1/fault_status_5_inverter"              : ( 60, 0 ),
    "pv/solis1/inverter_status"                      : ( 60, 0 ),
    "pv/solis1/operating_status"                     : ( 60, 0 ),
    "pv/solis1/rwr_backup_output_enabled"            : ( 60, 0 ),
    "pv/solis1/rwr_energy_storage_mode"              : ( 60, 0 ),

    # Solis1 Meter
    "pv/solis1/meter/export_active_energy"           : ( 60, 0.01 ),
    "pv/solis1/meter/import_active_energy"           : ( 60, 0.01 ),

    "pv/cpu_temp_c"                                  : ( 60, 1   ),
    "pv/disk_space_gb"                               : ( 60, 0.1 ),
    "pv/cpu_load_percent"                            : ( 60, 1   ),
}

for k,v in tuple(MQTT_RATE_LIMIT.items()):
    if k.startswith("pv/solis1/"):
        MQTT_RATE_LIMIT[k.replace("pv/solis1/","pv/solis2/")] = v


