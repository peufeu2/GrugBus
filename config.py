#!/usr/bin/python
# -*- coding: utf-8 -*-

import re

from config_secret import *
from misc.interpolate import Interp

# config_secret.py is not included on github and should contain:
"""
#!/usr/bin/python
# -*- coding: utf-8 -*-

# Fill in the blanks
MQTT_USER       = 
MQTT_PASSWORD   = 
CLICKHOUSE_USER = 
CLICKHOUSE_PASSWORD = 
"""

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

CLICKHOUSE_INSERT_PERIOD_SECONDS = 5    # pool mqtt data and bulk insert into database every ... seconds

##################################################################
# MQTT Buffer
##################################################################

# MQTT Buffer/logger server
MQTT_BUFFER_IP   = SOLARPI_IP
MQTT_BUFFER_PORT = 15555
MQTT_BUFFER_RETENTION = 24*3600*365 # how long to keep log files
MQTT_BUFFER_FILE_DURATION = 3600	# number of seconds before new log file is created

# path on solarpi for storage of mqtt compressed log
MQTT_BUFFER_PATH = "/home/peufeu/mqtt_buffer"
# temporary path on PC with clickhouse to copy logs and insert into database
MQTT_BUFFER_TEMP = "/mnt/ssd/temp/solarpi/mqtt"

##################################################################
# Modbus configuration
##################################################################
#
#   Use by-id so the ports don't move around after a plug and pray session
#   Note: FTDI FT_PROG utility can change serial number in FT2232 EEPROM, which
#   allows renaming serial ports.

_SERIAL_DEFAULTS = {
    "timeout"  : 0.5,
    "retries"  : 1,
    "baudrate" : 9600,
    "bytesize" : 8,
    "parity"   : "N",
    "stopbits" : 1,
}

SOLIS = {
    "solis1":  {
        "CAN_PORT"   : 'can_1',
        "SERIAL"     : _SERIAL_DEFAULTS | { "port"   : "/dev/serial/by-id/usb-FTDI_USB_RS485_1-if01-port0" },   # Solis1 COM port
        "PARAMS": {
            "modbus_addr" : 1,
            "key"         : "solis1",
            "name"        : "Solis 1",    
        },
        "FAKE_METER" : {
            "port"            : "/dev/serial/by-id/usb-FTDI_USB_RS485_1-if00-port0",   # Solis1 fakemeter
            "baudrate"        : 9600, 
            "key"             : "fake_meter_1", 
            "name"            : "Fake meter for Solis 1", 
            "modbus_address"  : 1, 
        },
        "LOCAL_METER" : { 
            "SERIAL": _SERIAL_DEFAULTS | { "port" : "/dev/serial/by-id/usb-FTDI_USB_RS485_4-if00-port0" },
            "PARAMS": {
                "modbus_addr" : 3, 
                "key"         : "ms1", 
                "name"        : "Smartmeter on Solis 1", 
            }
        },
    },
    "solis2": {
        "CAN_PORT"   : 'can_2',
        "SERIAL"     : _SERIAL_DEFAULTS | { "port"   : "/dev/serial/by-id/usb-FTDI_USB_RS485_2-if01-port0" },   # Solis2 COM port
        "PARAMS": {
            "modbus_addr" : 1,
            "key"         : "solis2",
            "name"        : "Solis 2",    
        },
        "FAKE_METER" : {
            "port"            : "/dev/serial/by-id/usb-FTDI_USB_RS485_2-if00-port0",   # Solis1 fakemeter
            "baudrate"        : 9600, 
            "key"             : "fake_meter_2",
            "name"            : "Fake meter for Solis 2",
            "modbus_address"  : 1, 
        },
        "LOCAL_METER" : { 
            "SERIAL": _SERIAL_DEFAULTS | { "port" : "/dev/serial/by-id/usb-FTDI_USB_RS485_4-if01-port0" },
            "PARAMS": {
                "modbus_addr" : 1,
                "key"         : "ms2",
                "name"        : "Smartmeter on Solis 2",
            }
        }
    }
}

METER = {
    "SERIAL": _SERIAL_DEFAULTS | {
        "port"     : "/dev/serial/by-id/usb-FTDI_USB_RS485_3-if01-port0",
        "baudrate" : 19200,
    },
    "PARAMS": {
        "modbus_addr" : 1,
        "key"         : "meter",
        "name"        : "SDM630 Smartmeter"
    }
}

# Fake meter safe mode: ignore or abort if data has not been updated since ... seconds
FAKE_METER_MAX_AGE_IGNORE = 1.5     
FAKE_METER_MAX_AGE_ABORT  = 2.5

EVSE = {
    "SERIAL": _SERIAL_DEFAULTS | { 
        "port"    : "/dev/serial/by-id/usb-FTDI_USB_RS485_3-if00-port0",        
        "timeout" : 2,    
    },
    "PARAMS": {
        "modbus_addr" : 3,
        "key"         : "evse",
        "name"        : "ABB Terra", 
    },
    "LOCAL_METER" : { 
        # No serial definition as it uses the same port as EVSE
        "PARAMS": {
            "modbus_addr" : 1, 
            "key"         : "mevse", 
            "name"        : "SDM120 Smartmeter on EVSE"
        }
    }
}

##################################################################
# CAN
##################################################################

CAN_PORT_BATTERY  = 'can_bat'

# How often we send modbus requests to these devices, in seconds
#
POLL_PERIOD_METER       = 0.2
POLL_PERIOD_SOLIS_METER = 0.2
POLL_PERIOD_EVSE_METER  = 0.2
POLL_PERIOD_SOLIS       = 0.2
POLL_PERIOD_EVSE        = 1

##################################################################
# Measurement offset correction
##################################################################

def solis2_calibrate_ibat( ibat ):
    if ibat<0:
        ibat += 1.3
        if ibat < -20:
            ibat *= 0.97
    return ibat

CALIBRATION = {
    # Inverter internal current measurement offset
    # If measured value is exactly zero, keep it, otherwise add offset
    "pv/solis2/battery_current" : solis2_calibrate_ibat,
}

##################################################################
# Power saving mode (turn off invertes) in pv_controller.py
##################################################################

# Inverter auto turn on/off settings
SOLIS_POWERSAVE_CONFIG = {
    "solis1": {
        "ENABLE_INVERTER"      : True,       # If False, turn inverter off
        "ENABLE_POWERSAVE"     : True,       # If True, enable following logic:
        "TURNOFF_BATTERY_SOC"  : 95,         # turn it off when SOC < value
        "TURNOFF_MPPT_VOLTAGE" : 50,         # ...and MPPT voltage < value
        "TURNON_MPPT_VOLTAGE"  : 80,         # turn it back on when MPPT voltage > value
    },
    "solis2": {
        "ENABLE_INVERTER"      : True,
        "ENABLE_POWERSAVE"     : True,
        "TURNOFF_BATTERY_SOC"  : 8 ,  
        "TURNOFF_MPPT_VOLTAGE" : 50,  
        "TURNON_MPPT_VOLTAGE"  : 80,  
    }    
}

##################################################################
# Power Router configuration
##################################################################

# "default" is  automatically added to the runtime-selected configuration, no need to copy these settings
# Note EVSE force charge will override these settings
#
ROUTER = {
    #
    #   Default configuration: priority to battery charging, then EV, then the rest.
    #
    "default": {
        #
        # configuration for router itself
        #
        "router": {
            # For smartplugs: try to not wear out relays
            # minimum on time and minimum off time
            "plugs_min_on_time_s"   : 5     ,
            "plugs_min_off_time_s"  : 20    ,

            # Set target export power for router
            "p_export_target"       : lambda soc: 100 + soc*1.0,

            # When battery is inactive, we pretend its current is zero,
            # which removes BMS offset error
            "battery_active"        : lambda mgr:  not (-2.0 < mgr.bms_current.value < 1.0),

            # Result of previous function is averaged then compared to this
            "battery_active_threshold" : 0.1,

            #   Detect when the inverter won't charge, even when it reports
            #   battery max charge current not being zero
            "battery_full"          : lambda mgr, battery_active: (
                   mgr.battery_max_charge_power==0 
                # or mgr.meter_power_tweaked < -300 
                or (mgr.bms_soc.value > 98 and not battery_active)
            ),

            # Result of previous function is averaged then compared to this
            "battery_full_threshold" : 0.9,
            
            # Moving averages length (seconds)
            "smooth_export_time_window"      : 1,    
            "smooth_bp_time_window"          : 1,
            "battery_active_avg_time_window" : 20,            
            "battery_full_avg_time_window"   : 20,        

            # Changes are commited if they are confirmed for this long,
            # to avoid triggering on spikes
            "confirm_change_time"            : 1.5,

            # Detect MPPT recalibration causing transient power drops (see router.py)
            "mppt_drop_power_threshold"      : 0.2,
            "mppt_drop_max_duration"         : 3.5,
            "mppt_drop_average_duration"     : 20,

        },
        #
        #   EVSE must always have higher priority than battery.
        #   EVSE decides how much it leaves to the battery via "battery_interp" setting.
        "evse"      : { 
            "priority"                  : 4, 
            "name"                      : "EVSE", 
            "enabled"                   : True,

            # These settings are placeholders, they're overwritten by EVSE configuration below
            "high_priority_W"       : lambda ctx: 0,
            "reserve_for_battery_W"       : lambda ctx: 10000,
            "start_threshold_W"         : lambda ctx: 1500, 
            "stop_threshold_W"          : lambda ctx: 1400, 

            "start_time_s"              : 120,      # how long above minimum excess power before starting
            "stop_time_s"               : 600,      # how long before stopping when we don't have enough power

            "charge_detect_threshold_W" : 1200,     # detect beginning and end of charge

            "command_interval_s"        : 1       ,     # minimum time to wait between current adjustments
            "command_interval_small_s"  : 10      ,     # minimum time to wait between small current adjustments
            "small_current_step_A"      : 1       ,     # any adjustment lower than this is small

            "up_timeout_s"              : 15      ,     # after lowering current, wait before raising it
            "end_of_charge_timeout_s"   : 240     ,     # time spent with current lower than charge_detect before we decide it's finished
            "power_report_timeout_s"    : 5       ,     # after making a change, report power according to the change during this interval, then switch to meter reading
            "dead_band_W"               : 0.5*240 ,     # power dead band where no adjustments are made
            "stability_threshold_W"     : 250     ,     # consider meter readings stable if they fluctuate less than this

            "control_gain_p"            : 0.96    ,     # control loop gain, must be <1
            "control_gain_i"            : 0.05    ,     # gain for the integrator in the PI loop
        },

    # Battery config: 
    #   At min_soc, allocate max_power to battery.
    #   At max_soc, allocate min_power to battery.
    #   Interpolate in between.
        "bat"       : { "priority": 3, "name": "Battery", "enabled": True, "power_func": Interp((95,10000),(100,1000),var="soc") },

    # Plugs config: 
    #   "estimated_power"   : estimation of power before it is measured at first turn on
    #   "min_power"         : ignore power measurements below this value
    #   "hysteresis"        : on/off power hysteresis
        "tasmota_t4": { "priority": 2, "enabled": True, "estimated_power": 1000 , "min_power": 500, "hysteresis": 50, "plug_topic": "plugs/tasmota_t4/", "name": "Tasmota T4 Sèche serviette"  },
        "tasmota_t2": { "priority": 1, "enabled": True, "estimated_power":  800 , "min_power": 500, "hysteresis": 50, "plug_topic": "plugs/tasmota_t2/", "name": "Tasmota T2 Radiateur PF"     },
        "tasmota_t1": { "priority": 0, "enabled": True, "estimated_power":  800 , "min_power": 500, "hysteresis": 50, "plug_topic": "plugs/tasmota_t1/", "name": "Tasmota T1 Radiateur bureau" },
    },

    "evse_low": {
        "evse": {
            #
            #   EVSE/Battery power management:
            #   First excess power is computed. Then, EVSE takes up to high_priority_W, if available.
            #   This is to avoid frequent start/stop cycles. This should be > start_threshold_W.
            "high_priority_W"       : Interp((90, 0), (91, 1500), var="soc"),    

            # Remaining excess goes to battery up to reserve_for_battery_W
            # Interp(min_soc, max_power, max_soc, min_power) 
            "reserve_for_battery_W" : Interp((90, 10000), (95, 0), var="soc"),
            # Then what remains after that goes to EVSE.

            "start_threshold_W"     : Interp((90, 1400), (100, 1200),var="soc"), # minimum excess power to start charging
            "stop_threshold_W"      : Interp((90, 1400), (100,  800),var="soc"), # excess power to stop charging
        }
    },

    #   Charge the car and battery at the same time to maximize self consumption
    #
    #   Allocate "high_priority_W" watts to EV charging (if available)
    #   then "reserve_for_battery_W" to battery, then the rest to EV.
    #   Relaxed start/stop thresholds, allowing to discharge battery a little
    #   to avoid stopping charge on each cloud.
    "evse_high": { 
        "evse": {
            "high_priority_W"       : lambda ctx: 2000, 
            "reserve_for_battery_W" : Interp((50, 6000),  (95, 1000),var="soc"),
            "start_threshold_W"     : Interp((50, 2000), (100, 1200),var="soc"),
            "stop_threshold_W"      : Interp((70, 1400), (100,  800),var="soc"),     # allow it to discharge battery a little
        },
    },

    #   Maximum PV power for EV, allows a bit of battery discharge.
    #   For more power to EV, use force charge.
    #
    "evse_max": { 
        "evse": {
            "high_priority_W"       : lambda ctx: 2000, 
            "reserve_for_battery_W" : lambda ctx: 0,
            "start_threshold_W"     : Interp((50, 1400), (100, 1000),var="soc"),
            "stop_threshold_W"      : Interp((50, 1000), (100,  500),var="soc"),
        },
    },
}

ROUTER_DEFAULT_CONFIG = ["default", "evse_high"]



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

    #   PV Controller
    #

    'pv/solis1/fakemeter/lag'                       : (  10,       1.000, 'avg'   ), #  0.026/14.297,

    # Compress/threshold heavily
    'pv/meter/is_online'                            : (  60,      0.000, ''      ), #  0.021/ 5.011,

    # Published every minute, do not limit
    'pv/solis1/fakemeter/req_per_s'                 : (  30,      0.000, ''      ), #  0.021/ 0.021,

    # Master process needs every value, don't limit it, so set margin to -1
    'pv/meter/total_power'                          : (   1,     25.000, 'avg'      ), #  4.736/ 4.995,

    # This is for debugging only and generates huge traffic, average it
    'pv/solis1/fakemeter/active_power'              : (   1,     25.000, ''      ), #  4.714/ 4.974,

    # Average voltage
    'pv/meter/phase_1_line_to_neutral_volts'        : (  10,      1.500, 'avg'   ), #  1.250/ 1.250,
    'pv/meter/phase_2_line_to_neutral_volts'        : (  10,      1.500, 'avg'   ), #  1.250/ 1.250,
    'pv/meter/phase_3_line_to_neutral_volts'        : (  10,      1.500, 'avg'   ), #  1.250/ 1.250,

    # Master needs real time current to avoid tripping breakers
    'pv/meter/phase_1_current'                      : (   1,      1.000, 'avg'   ), #  1.240/ 1.250,
    'pv/meter/phase_2_current'                      : (   1,      1.000, 'avg'   ), #  1.240/ 1.250,
    'pv/meter/phase_3_current'                      : (   1,      1.000, 'avg'   ), #  1.250/ 1.250,

    # Per-phase power is useful, don't reduce it too much
    'pv/meter/phase_1_power'                        : (   1,     50.000, ''      ), #  1.250/ 1.250,
    'pv/meter/phase_2_power'                        : (   1,     50.000, ''      ), #  1.250/ 1.250,
    'pv/meter/phase_3_power'                        : (   1,     50.000, ''      ), #  1.250/ 1.250,

    # Only for logging purposes, average it
    'pv/meter/total_volt_amps'                      : (  10,    100.000, 'avg'      ), #  1.250/ 1.250,
    'pv/meter/total_var'                            : (  10,    100.000, 'avg'      ), #  1.250/ 1.250,
    'pv/meter/total_power_factor'                   : (  10,   1000.000, 'avg'      ), #  1.250/ 1.250,
    'pv/meter/total_phase_angle'                    : (  10,   1000.000, 'avg'      ), #  0.996/ 1.250,

    # Energy: use threshold
    'pv/meter/total_import_kwh'                     : (  60,      0.01 , ''      ), #  0.074/ 1.250,
    'pv/meter/total_export_kwh'                     : (  60,      0.01 , ''      ), #  0.085/ 1.250,
    'pv/meter/total_import_kvarh'                   : (  60,      0.01 , ''      ), #  0.026/ 1.250,
    'pv/meter/total_export_kvarh'                   : (  60,      0.01 , ''      ), #  0.037/ 1.250,
    'pv/meter/total_kwh'                            : (  60,      0.01 , ''      ), #  0.079/ 1.245,
    'pv/meter/total_kvarh'                          : (  60,      0.01 , ''      ), #  0.058/ 1.245,

    # Average these
    'pv/meter/average_line_to_neutral_volts_thd'    : (  10,      100.0, 'avg'   ), #  1.250/ 1.250,
    'pv/meter/average_line_current_thd'             : (  10,      100.0, 'avg'   ), #  1.250/ 1.250,
    'pv/meter/frequency'                            : (  10,      1.000, 'avg'   ), #  1.234/ 1.250,

    # Low rate, publish as-is
    'pv/disk_space_gb'                              : (  0,      0.000, ''      ), #  0.021/ 0.117,
    'pv/cpu_temp_c'                                 : (  0,      0.000, ''      ), #  0.085/ 0.101,
    'pv/cpu_load_percent'                           : (  0,      0.000, ''      ), #  0.090/ 0.095,


    #   CANBUS BMS INFORMATION
    #
    # Publish all on change
    'pv/bms/soc'                                  : (   2,      0.000, ''      ), #  0.483/ 0.981,
    'pv/bms/voltage'                              : (   1,      0.000, ''      ), #  0.479/ 0.917,
    'pv/bms/current'                              : (   1,      0.000, ''      ), #  0.481/ 0.902,
    'pv/bms/power'                                : (   1,      0.000, ''      ), #  0.482/ 0.889,
    'pv/bms/max_charge_voltage'                   : (  10,      0.000, ''      ), #  0.017/ 0.516,
    'pv/bms/max_discharge_current'                : (  10,      0.000, ''      ), #  0.017/ 0.516,
    'pv/bms/protection'                           : (  10,      0.000, ''      ), #  0.017/ 0.516,
    'pv/bms/alarm'                                : (  10,      0.000, ''      ), #  0.017/ 0.516,
    'pv/bms/soh'                                  : (  10,      0.000, ''      ), #  0.017/ 0.516,
    'pv/bms/request_full_charge'                  : (  10,      0.000, ''      ), #  0.017/ 0.516,
    'pv/bms/request_force_charge_2'               : (  10,      0.000, ''      ), #  0.017/ 0.516,
    'pv/bms/request_force_charge_1'               : (  10,      0.000, ''      ), #  0.017/ 0.516,
    'pv/bms/discharge_enable'                     : (  10,      0.000, ''      ), #  0.017/ 0.516,
    'pv/bms/charge_enable'                        : (  10,      0.000, ''      ), #  0.017/ 0.516,
    'pv/bms/max_charge_current'                   : (  10,      0.000, ''      ), #  0.017/ 0.515,
    'pv/bms/temperature'                          : (  10,      0.000, ''      ), #  0.026/ 0.514,
    'pv/bms/max_charge_power'                     : (  10,      0.000, ''      ), #  0.062/ 0.510,
    'pv/bms/max_discharge_power'                  : (  10,      0.000, ''      ), #  0.069/ 0.508,
    'pv/bms/charge_current_limit'                 : (  10,      0.000, ''      ), #  0.000/ 0.000,
    'pv/bms/discharge_current_limit'              : (  10,      0.000, ''      ), #  0.000/ 0.000,

    #   PV Controller
    #

    'pv/evse/meter/active_power'               : (   1,     50.000, 'avg'      ), #  0.019/ 0.937,
    'pv/evse/rwr_current_limit'                : (  60,      0.000, ''      ), #  0.019/ 0.112,

    'pv/evse/energy'                           : (  60,      0.010, ''      ), #  0.019/ 1.012,
    'pv/evse/meter/export_active_energy'       : (  600,     1.000, ''      ), #  0.019/ 0.099,
    'pv/evse/meter/import_active_energy'       : (  60,      0.010, ''      ), #  0.019/ 0.099,

    'pv/evse/meter/voltage'                    : (  60,      1.000, 'avg'      ), #  0.341/ 0.838,

    'pv/router/evse/state'                     : (60.000000, 0.000, ''      ), #  0.204/ 4.087,
    'pv/evse/error_code'                       : (60.000000, 0.000, ''      ), #  0.204/ 1.022,
    'pv/evse/socket_state'                     : (60.000000, 0.000, ''      ), #  0.204/ 1.022,
    'pv/evse/charge_state'                     : (60.000000, 0.000, ''      ), #  0.204/ 1.022,
    'pv/evse/current_limit'                    : (60.000000, 0.000, ''      ), #  0.204/ 1.022,


    'pv/meter/house_power'                     : (   1,     40.000, 'avg'      ), #  4.711/ 4.743,
    'pv/total_battery_power'                   : (   1,     40.000, 'avg'      ), #  1.074/ 4.743,
    'pv/total_grid_port_power'                 : (   1,     40.000, 'avg'      ), #  4.227/ 4.743,
    'pv/total_input_power'                     : (   1,     40.000, 'avg'      ), #  4.227/ 4.743,
    'pv/total_pv_power'                        : (   1,     40.000, 'avg'      ), #  0.019/ 4.755,

    'pv/router/excess_avg'                     : (   1,     25.000, 'avg'      ), #  4.463/ 4.463,

    'pv/solis1/input_power'                    : (   1,     25.000, 'avg'      ), #  4.165/ 4.743,
    'pv/solis1/meter/active_power'             : (   1,     25.000, 'avg'      ), #  4.674/ 4.978,
    'pv/solis1/pv_power'                       : (   1,     25.000, 'avg'      ), #  0.019/ 1.825,

    'pv/solis1/battery_current'                : (   1,      0.200, 'avg'      ), #  0.534/ 1.813,
    'pv/solis1/battery_power'                  : (   1,     25.000, 'avg'      ), #  0.534/ 1.813,
    'pv/solis1/battery_voltage'                : (  10,      0.100, 'avg'      ), #  0.031/ 0.279,

    'pv/solis1/backup_load_power'              : (  60,     50.000, 'avg'      ), #  0.019/ 0.279,
    'pv/solis1/backup_voltage'                 : (  60,      1.000, 'avg'      ), #  0.149/ 0.267,
    'pv/solis1/battery_charge_energy_today'    : (  60,      0.100, ''      ), #  0.019/ 0.279,
    'pv/solis1/battery_discharge_energy_today' : (  60,      0.100, ''      ), #  0.019/ 0.279,
    'pv/solis1/energy_generated_today'         : (  60,      0.100, ''      ), #  0.019/ 0.279,
    'pv/solis1/energy_generated_yesterday'     : (  60,      0.100, ''      ), #  0.019/ 0.279,
    'pv/solis1/meter/export_active_energy'     : (  60,      0.100, ''      ), #  0.056/ 0.497,
    'pv/solis1/meter/import_active_energy'     : (  60,      0.100, ''      ), #  0.019/ 0.509,

    "pv/energy_generated_today"                : (  60,      0.100, ''      ),
    "pv/battery_charge_energy_today"           : (  60,      0.100, ''      ),

    'pv/solis1/mppt1_current'                  : (   2,      0.100, 'avg'      ), #  0.019/ 1.825,
    'pv/solis1/mppt2_current'                  : (   2,      0.100, 'avg'      ), #  0.019/ 1.825,
    'pv/solis1/mppt1_power'                    : (   2,     25.000, 'avg'      ), #  0.019/ 1.825,
    'pv/solis1/mppt2_power'                    : (   2,     25.000, 'avg'      ), #  0.019/ 1.825,
    'pv/solis1/mppt1_voltage'                  : (  10,      2.000, 'avg'      ), #  0.453/ 1.813,
    'pv/solis1/mppt2_voltage'                  : (  10,      2.000, 'avg'      ), #  0.081/ 1.819,

    'pv/solis1/phase_a_voltage'                : (  60,      2.000, 'avg'      ), #  0.161/ 0.267,
    'pv/solis1/temperature'                    : (  60,      0.200, 'avg'      ), #  0.068/ 0.273,

    # publish only on change
    'pv/battery_max_charge_power'              : (  60,    100.000, ''      ), #  0.050/ 4.749,
    'pv/solis1/battery_max_charge_current'     : (  60,      0.100, ''      ), #  0.019/ 0.273,
    'pv/solis1/battery_max_discharge_current'  : (  60,      0.100, ''      ), #  0.019/ 0.273,
    'pv/solis1/fault_status_1_grid'            : (  60,      0.000, ''      ), #  0.031/ 0.279,
    'pv/solis1/fault_status_2_backup'          : (  60,      0.000, ''      ), #  0.019/ 0.279,
    'pv/solis1/fault_status_3_battery'         : (  60,      0.000, ''      ), #  0.019/ 0.279,
    'pv/solis1/fault_status_4_inverter'        : (  60,      0.000, ''      ), #  0.019/ 0.279,
    'pv/solis1/fault_status_5_inverter'        : (  60,      0.000, ''      ), #  0.019/ 0.279,
    'pv/solis1/inverter_status'                : (  60,      0.000, ''      ), #  0.031/ 0.279,
    'pv/solis1/operating_status'               : (  60,      0.000, ''      ), #  0.118/ 0.267,
    'pv/solis1/rwr_backup_output_enabled'      : (  60,      0.000, ''      ), #  0.019/ 0.273,
    'pv/solis1/rwr_energy_storage_mode'        : (  60,      0.000, ''      ), #  0.019/ 0.273,
    'pv/solis1/rwr_power_on_off'               : (  60,      0.000, ''      ), #  0.019/ 0.273,

    # smartplugs
    'cmnd/plugs/tasmota_t1/Power'              : (60.000000,      0.000, ''      ), #  0.016/ 0.473,
    'cmnd/plugs/tasmota_t2/Power'              : (60.000000,      0.000, ''      ), #  0.016/ 0.473,
    'cmnd/plugs/tasmota_t3/Power'              : (60.000000,      0.000, ''      ), #  0.016/ 0.473,
    'cmnd/plugs/tasmota_t4/Power'              : (60.000000,      0.000, ''      ), #  0.016/ 0.473,
    'cmnd/plugs/tasmota_t5/Power'              : (60.000000,      0.000, ''      ), #  0.016/ 0.473,
    'cmnd/plugs/tasmota_t6/Power'              : (60.000000,      0.000, ''      ), #  0.016/ 0.473,

    # router
    "pv/router/excess"                         : (60, 150.00, 'avg' ),
    "pv/router/bat/max_power"                  : (60, 50.000, '' ),

    # evse router settings, set by config
    "pv/router/evse/high_priority_W"       : (600, 50.000, '' ),
    "pv/router/evse/reserve_for_battery_W"       : (600, 50.000, '' ),
    "pv/router/evse/start_threshold_W"         : (600, 50.000, '' ),
    "pv/router/evse/stop_threshold_W"          : (600, 50.000, '' ),


}

for k,v in tuple(MQTT_RATE_LIMIT.items()):
    if k.startswith("pv/solis1/"):
        MQTT_RATE_LIMIT[k.replace("pv/solis1/","pv/solis2/")] = v

###############################################################
#
#   mqtt_buffer.py configuration
#
###############################################################

#   When a topic matches and the payload is JSON {dict}, mqtt_buffer
#   will unwrap the dict and republish only contents specified here:
#
MQTT_BUFFER_FILTER = [
    ( re.compile( r"^tele/plugs/tasmota_t.*?/STATE$" ), {} ),
    ( re.compile( r"^stat/plugs/tasmota_t.*?/RESULT$" ), {"POWER": ( lambda s:int(s=="ON"), ( 60, 0.000, '' )) } ),
    ( re.compile( r"^tele/plugs/tasmota_t.*?/SENSOR$" ), {"ENERGY":{"Power": ( float, (10, 20, "avg")) }} ),
    ( re.compile( r"^stat/plugs/tasmota_t.*?/STATUS8$" ), {"StatusSNS":{"ENERGY":{"Power":(float, (1, 20, "avg")) }}} ),
]
