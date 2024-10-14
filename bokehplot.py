import random, time, clickhouse_driver, collections, pprint, re
import asyncio, time, traceback, logging, sys, datetime
import numpy as np

from tornado.ioloop import IOLoop
import bokeh.events, bokeh.layouts, bokeh.plotting
from bokeh.server.server import Server
from bokeh.application import Application
from bokeh.application.handlers.function import FunctionHandler
from bokeh.models import CustomJS, Slider, TabPanel, Tabs
import bokeh.models.widgets
from gmqtt import Client as MQTTClient

import config
from misc import *

STREAMING_LENGTH = 1200         # seconds
STREAMING_LENGTH_MIN = 200     # seconds
STREAMING_LENGTH_MAX = 3600    # seconds
TIME_SHIFT_S = 3600*2          # to shift database timestamps stored in UTC
TIME_SHIFT = np.timedelta64( TIME_SHIFT_S, 's' )

clickhouse = clickhouse_driver.Client('localhost', user=config.CLICKHOUSE_USER, password=config.CLICKHOUSE_PASSWORD )

# Category20 colors: https://docs.bokeh.org/en/latest/docs/reference/palettes.html
cat20 = ('#1f77b4', '#aec7e8', '#ff7f0e', '#ffbb78', '#2ca02c', '#98df8a', '#d62728', '#ff9896', '#9467bd', '#c5b0d5', '#8c564b', '#c49c94', '#e377c2', '#f7b6d2', '#7f7f7f', '#c7c7c7', '#bcbd22', '#dbdb8d', '#17becf', '#9edae5')
def _nextcolor():
    while True:
        for _ in cat20:
            yield _
_nextcolor = _nextcolor()
def nextcolor():
    return next( _nextcolor )

display_bools_offset = 0
def display_bools():
    global display_bools_offset
    o = display_bools_offset
    display_bools_offset += 2
    return lambda y: y+o

colors = {
    "pv"            : ["#00FF00", "#00C000", "#008000"],
    "mppt1"         : [None     , nextcolor(), nextcolor()],
    "mppt2"         : [None     , nextcolor(), nextcolor()],
    "battery"       : ["#FF8080", "#C0A060", "#807040"],
    "bms"           : ["#FFC080", "#C0A060", "#807040"],
    "fakemeter"     : ["#FFFFFF", "#C0C0C0", "#808080"],
    "grid_port"     : ["cyan"   , "#00C0C0", "#008080"],
    "input"         : ["#FFFF00", "#C0C000", "#808000"],
    "soc"           : ["#00FF00", "#00C000", "#008000"],
    "temperature"   : ["cyan"   , "#00C0C0", "#008080"],
}
color_phase1 = "#FF0000"
color_phase2 = "#00FF00"
color_phase3 = "#FFFF00"

# For window title
# TITLE_DATA = { k:None for k in (
#     "pv/solis1/bms_battery_soc",
#     "pv/meter/total_export_kwh",
#     "pv/meter/total_import_kwh",
#     )}

#   ( topic, unit, label, color, dash, scale, attrs ) < must be tuples
#
DATA_STREAMS = [
    # PV
    ( "pv/total_pv_power"                                , "W"   , "Total PV"                    , "pv"            , "solid"    , 1.0 , {}                 , {} ),
    ( "pv/solis%d/pv_power"                              , "W"   , "S%d PV"                      , "pv"            , "solid"    , 1.0 , {}                 , {} ),
    ( "pv/solis%d/mppt1_power"                           , "W"   , "S%d MPPT1"                   , "mppt1"         , "solid"    , 1.0 , {}                 , {} ),
    ( "pv/solis%d/mppt2_power"                           , "W"   , "S%d MPPT2"                   , "mppt2"         , "solid"    , 1.0 , {}                 , {} ),
    ( "pv/solis%d/mppt1_current"                         , "A"   , "S%d MPPT1"                   , "mppt2"         , "solid"    , 1.0 , {}                 , {} ),
    ( "pv/solis%d/mppt2_current"                         , "A"   , "S%d MPPT2"                   , "mppt1"         , "solid"    , 1.0 , {}                 , {} ),
    ( "pv/solis%d/mppt1_voltage"                         , "V"   , "S%d MPPT1"                   , "mppt1"         , "solid"    , 1.0 , {}                 , {} ),
    ( "pv/solis%d/mppt2_voltage"                         , "V"   , "S%d MPPT2"                   , "mppt2"         , "solid"    , 1.0 , {}                 , {} ),

    # Meter
    ( "pv/meter/total_power"                             , "W"   , "Grid"                        , "#FF0000"       , "solid"    , 1.0 , {}                 , {} ),
    ( "pv/meter/house_power"                             , "W"   , "House"                       , "#8080FF"       , "solid"    , 1.0 , {}                 , {} ),
    ( "pv/meter/phase_1_power"                           , "W"   , "Phase 1"                     , color_phase1    , "solid"    , 1.0 , {}                 , {} ),
    ( "pv/meter/phase_2_power"                           , "W"   , "Phase 2"                     , color_phase2    , "solid"    , 1.0 , {}                 , {} ),
    ( "pv/meter/phase_3_power"                           , "W"   , "Phase 3"                     , color_phase3    , "solid"    , 1.0 , {}                 , {} ),
    ( "pv/meter/phase_1_line_to_neutral_volts"           , "V"   , "Phase 1"                     , color_phase1    , "solid"    , 1.0 , {}                 , {} ),
    ( "pv/meter/phase_2_line_to_neutral_volts"           , "V"   , "Phase 2"                     , color_phase2    , "solid"    , 1.0 , {}                 , {} ),
    ( "pv/meter/phase_3_line_to_neutral_volts"           , "V"   , "Phase 3"                     , color_phase3    , "solid"    , 1.0 , {}                 , {} ),
    ( "pv/meter/phase_1_current"                         , "A"   , "Phase 1"                     , color_phase1    , "solid"    , 1.0 , {}                 , {} ),
    ( "pv/meter/phase_2_current"                         , "A"   , "Phase 2"                     , color_phase2    , "solid"    , 1.0 , {}                 , {} ),
    ( "pv/meter/phase_3_current"                         , "A"   , "Phase 3"                     , color_phase3    , "solid"    , 1.0 , {}                 , {} ),

    ( "pv/meter/frequency"                               , "Hz"  , "Frequency"                   , nextcolor()     , "solid",    1.0, {}, {}),
    ( "pv/meter/req_period"                              , "s"   , "req_period"                  , "#FFFF00"       , "solid",    1.0 , {}                 , {"aggregate":"max"} ),

    # Totals across inverters
    ( "pv/total_input_power"                             , "W"   , "Battery Proxy"               , "input"         , "solid",    1.0 , {"visible":False}                 , {} ),
    ( "pv/total_grid_port_power"                         , "W"   , "Grid Ports"                  , "grid_port"     , "solid",    1.0 , {}                 , {} ),
    ( "pv/total_battery_power"                           , "W"   , "Battery"                     , "battery"       , "solid"    , 1.0 , {}                 , {} ),
    ( "pv/battery_max_charge_power"                      , "W"   , "Battery Max Charge"          , "battery"       , "dashed"   , 1.0 , {}                 , {} ),
    ( "pv/energy_generated_today"                        , "kWh" , "Energy Generated today"      , nextcolor()     , "solid"   , 1.0 , {}                 , {} ),
    ( "pv/battery_charge_energy_today"                   , "kWh" , "Energy Charge today"      , "battery"       , "solid"   , 1.0 , {}                 , {} ),

    # Per-inverter data
    ( "pv/solis%d/fakemeter/active_power"                , "W"   , "S%d FakeMeter"               , "fakemeter"     , "solid",    1.0 , {"visible":False}  , {} ),
    ( "pv/solis%d/fakemeter/lag"                         , "s"   , "S%d Fakemeter lag"           , nextcolor       , "solid",    1.0 , {"visible":False}  , {} ),
    ( "pv/solis%d/input_power"                           , "W"   , "S%d Battery Proxy"           , "input"         , "solid",    1.0 , {}                 , {} ),
    ( "pv/solis%d/meter/active_power"                    , "W"   , "S%d Grid port"               , "grid_port"     , "solid",    1.0 , {}                 , {} ),
  # ( "pv/solis%d/dc_bus_voltage"                        , "V"   , "S%d DC Bus"                  , (cat20,8)       , "solid",    1.0 , {}                 , {} ),
    ( "pv/solis%d/temperature"                           , "°C"  , "S%d Temperature"             , "temperature"   , "solid",    1.0 , {}                 , {} ),
    ( "pv/solis%d/fan_rpm"                               , "rpm/100"  , "S%d Fan RPM"            , nextcolor       , "solid",    0.01, {"visible":False}                 , {} ),
    # ( "pv/solis%d/energy_generated_today"                , "kWh" , "S%d Energy generated"        , "pv"            , "solid",    1.0 , {}                 , {"mode":"delta"} ),
    ( "pv/evse/meter/import_active_energy"               , "kWh" , "EVSE meter"                  , "#FFC0FF"       , "solid",    1.0 , {"visible":False}  , {"mode":"delta"} ),
    ( "pv/solis%d/meter/import_active_energy"            , "kWh" , "S%d Import"                  , "input"         , "solid",    1.0 , {"visible":False}  , {"mode":"delta"} ),
    ( "pv/solis%d/meter/export_active_energy"            , "kWh" , "S%d Export"                  , "grid_port"     , "solid",    1.0 , {"visible":False}  , {"mode":"delta"} ),

    # Battery (inverter side)
    ( "pv/solis%d/battery_power"                         , "W"   , "S%d Battery"                 , "battery"       , "solid"    , 1.0 , {"visible":False}  , {} ),
    ( "pv/solis%d/battery_current"                       , "A"   , "S%d Bat current"             , "battery"       , "solid"    , 1.0 , {"visible":False}  , {} ),
    ( "pv/solis%d/battery_voltage"                       , "V"   , "S%d Bat voltage"             , "battery"       , "solid"    , 1.0 , {"visible":False}  , {} ),

    ( "pv/solis%d/battery_max_charge_current"            , "A"   , "S%d Bat max charge"          , "battery"       , "dashed",   1.0 , {"visible":False}  , {} ),
    ( "pv/solis%d/battery_max_discharge_current"         , "A"   , "S%d Bat max discharge"       , "battery"       , "dotted",   1.0 , {"visible":False}  , {} ),
    # ( "pv/solis%d/bms_battery_charge_current_limit"      , "A"   , "S%d BMS max charge"          , "bms"       , "dashed",   1.0 , {}                 , {} ),
    # ( "pv/solis%d/bms_battery_discharge_current_limit"   , "A"   , "S%d BMS max discharge"       , "bms"       , "dotted",   1.0 , {}                 , {} ),

    # Solis1 history
    ( "pv/solis1/bms_battery_current"                   , "A"   , "S%d BMS Battery current"     , "bms"       , "solid",    1.0 , {"visible":False}  , {} ),
    ( "pv/solis1/bms_battery_soc"                       , "%"   , "S%d Battery SOC"             , "soc"           , "solid",    1.0 , {"visible":False}  , {} ),

    # BMS
    ( "pv/bms/power"                                     , "W"   , "BMS"                         , "bms"           , "solid"    , 1.0 , {}  , {} ),
    ( "pv/bms/max_charge_power"                          , "W"   , "BMS Max Charge"              , "bms"           , "dashed"   , 1.0 , {}                 , {} ),
    ( "pv/bms/current"                                   , "A"   , "BMS"                         , "bms"           , "solid"    , 1.0 , {"visible":False}  , {} ),
    ( "pv/bms/max_charge_current"                        , "A"   , "BMS Max Charge"              , "#008000"       , "dashed"   , 1.0 , {}                 , {} ),
    ( "pv/bms/voltage"                                   , "V"   , "BMS"                         , "bms"           , "solid"    , 1.0 , {"visible":False}  , {} ),
    ( "pv/bms/soc"                                       , "%"   , "BMS SOC"                     , "soc"           , "solid"    , 1.0 , {}                 , {} ),
    ( "pv/bms/charge_enable"                             , "?"   , "BMS Charge Enable"           , "#00FF00"       , "solid"    , 1.0 , {}                 , {} ),
    ( "pv/bms/temperature"                               , "°C"  , "Battery Temperature"         , "battery"       , "solid",    1.0 , {}                 , {} ),
    ( "pv/bms/max_charge_voltage"  , "V", "Bat max_charge_voltage", nextcolor, "solid", 1.0, {}, {} ),
    ( "pv/bms/protection"          , "?", "Bat protection",         nextcolor, "solid", 1.0, {}, {} ),
    ( "pv/bms/alarm"               , "?", "Bat alarm",              nextcolor, "solid", 1.0, {}, {} ),

    # Router
    # ( "pv/router/battery_min_charge_power"               , "W"   , "Battery Min Charge"          , "battery"       , "dashed",   1.0 , {}                 , {} ),

    # EVSE
    ( "pv/evse/energy"                                   , "kWh" , "EVSE"                        , "#FF80FF"       , "solid",    1.0 , {}  , {} ),
    ( "pv/evse/meter/active_power"                       , "W"   , "EVSE"                        , "#FF80FF"       , "solid",    1.0 , {}                 , {} ),
    ( "pv/evse/rwr_current_limit"                        , "W"   , "EVSE ILim"                   , "#FFFFFF"       , "solid",  235.0 , {"visible":False}  , {} ),
    ( "pv/router/evse/state"                             , ""    , "EVSE State"                  , "#FF80FF"       , "solid",    1.0 , {"visible":False}  , {} ),

    # ( "pv/evse/command_interval"                         , "s"   , "EVSE timeout L"              , "#FFFFFF"       , "dotted",  1000.0 , {"visible":False}  , {} ),
    # ( "pv/evse/command_interval_small"                   , "s"   , "EVSE timeout S"              , "#808080"       , "dotted",  1000.0 , {"visible":False}  , {} ),

    # Pi
    ( "pv/cpu_temp_c"                                    , "°C"  , "Pi CPU Temperature"          , "#FF0000"       , "solid",    1.0 , {}                 , {} ),
    ( "pv/cpu_load_percent"                              , "%"   , "Pi CPU Load"                 , "#00FF00"       , "solid",    1.0 , {}                 , {} ),
    ( "pv/disk_space_gb"                                 , "GB"  , "Pi Disk Space"               , "#0080FF"       , "solid",    1.0 , {}                 , {} ),

    # Heating                
    ( "chauffage/depart"             , "°C", "Départ"                   , "#FF0000"  , "solid",   1.0             , {"visible":False}, { }),
    ( "chauffage/retour"             , "°C", "Retour"                   , "#FF0000"  , "solid",   1.0             , {"visible":False}, { }),
    ( "chauffage/pac_depart"         , "°C", "PAC départ"               , "#FF8000"  , "solid",   1.0             , {"visible":False}, { }),
    ( "chauffage/pac_retour"         , "°C", "PAC retour"               , "#FF8000"  , "solid",   1.0             , {"visible":False}, { }),
    ( "chauffage/debit"              , "°C", "Débit"                    , "#FFFFFF"  , "solid",   1.0             , {"visible":False}, { }),
    ( "chauffage/pompe"              , "°C", "Pompe"                    , "#80FF80"  , "solid",   1.0             , {"visible":False}, { }),
    ( "chauffage/et_bureau"          , "°C", "Étage Bureau"             , "#800080"  , "solid",   1.0             , {"visible":False}, { }),
    ( "chauffage/et_pcbt_depart"     , "°C", "Étage PCBT depart"        , "#8000FF"  , "solid",   1.0             , {"visible":False}, { }),
    ( "chauffage/et_pcbt_retour"     , "°C", "Étage PCBT retour"        , "#8000FF"  , "solid",   1.0             , {"visible":False}, { }),
    ( "chauffage/et_pcbt_ambient"    , "°C", "Étage PCBT ambient"       , "#8000FF"  , "solid",   1.0             , {"visible":True }, { }),
    ( "chauffage/ext_parking"        , "°C", "Extérieur parking"        , "#808000"  , "solid",   1.0             , {"visible":True }, { }),
    ( "chauffage/ext_sous_balcon"    , "°C", "Extérieur sous balcon"    , "#808000"  , "solid",   1.0             , {"visible":True }, { }),
    ( "chauffage/rc_pc_cuisine"      , "°C", "RC Cuisine"               , "#0000FF"  , "solid",   1.0             , {"visible":True }, { }),
    ( "chauffage/rc_pc_pcbt_ambient" , "°C", "RC PCBT ambient"          , "#00FF00"  , "solid",   1.0             , {"visible":True }, { }),
    ( "chauffage/rc_pc_pcbt_depart"  , "°C", "RC PCBT depart"           , "#00FF00"  , "solid",   1.0             , {"visible":False}, { }),
    ( "chauffage/rc_pc_pcbt_retour"  , "°C", "RC PCBT retour"           , "#00FF00"  , "solid",   1.0             , {"visible":False}, { }),
    ( "chauffage/rc_pf_pcbt_ambient" , "°C", "RC PCBT2 ambient"         , "#0080FF"  , "solid",   1.0             , {"visible":True }, { }),
    ( "chauffage/rc_pf_pcbt_depart"  , "°C", "RC PCBT2 depart"          , "#0080FF"  , "solid",   1.0             , {"visible":False}, { }),
    ( "chauffage/rc_pf_pcbt_retour"  , "°C", "RC PCBT2 retour"          , "#0080FF"  , "solid",   1.0             , {"visible":False}, { }),
    ( "chauffage/pac_puits"          , "°C", "PAC puits"                , "#00FFFF"  , "solid",   1.0             , {"visible":False}, { }),
    ( "chauffage/pac_rejet"          , "°C", "PAC rejet"                , "#008080"  , "solid",   1.0             , {"visible":False}, { }),
    ( "chauffage/rc_pf_che"          , "°C", "RC Chauffe-eau"           , "#FF8000"  , "solid",   1.0             , {"visible":False}, { }),

    # Broker
    ( "$SYS/broker/load/messages/received/1min" , "msg/s", "MQTT received"    , "#00FF00"  , "solid",      1/60             , {}, { }),
    ( "$SYS/broker/load/messages/sent/1min"     , "msg/s", "MQTT sent"        , "#FF0080"  , "solid",      1/60             , {}, { }),
    ( "$SYS/broker/load/bytes/received/1min"    , "kB/s",  "MQTT received"       , "#00FF00"  , "solid",   1/(60*1024)      , {}, { }),
    ( "$SYS/broker/load/bytes/sent/1min"        , "kB/s",  "MQTT sent"           , "#FF0080"  , "solid",   1/(60*1024)      , {}, { }),


    # Router
    ( "cmnd/plugs/tasmota_t4/Power", "ON", "Tasmota T4 Sèche serviette"     , nextcolor(), "solid", 1, {}, {"func":display_bools()} ),
    ( "cmnd/plugs/tasmota_t2/Power", "ON", "Tasmota T2 Radiateur PF"        , nextcolor(), "solid", 1, {}, {"func":display_bools()} ),
    ( "cmnd/plugs/tasmota_t1/Power", "ON", "Tasmota T1 Radiateur bureau"    , nextcolor(), "solid", 1, {}, {"func":display_bools()} ),
    ( "pv/router/mppts_in_drop",     "ON", "MPPT Drops"                     , nextcolor(), "solid", 1, {"visible":False}, {} ),

    ( "tele/plugs/tasmota_t4/SENSOR/ENERGY/Power", "W", "Tasmota T4 Sèche serviette"     , nextcolor(), "solid", 1, {"visible":False}, {} ),
    ( "tele/plugs/tasmota_t2/SENSOR/ENERGY/Power", "W", "Tasmota T2 Radiateur PF"        , nextcolor(), "solid", 1, {"visible":False}, {} ),
    ( "tele/plugs/tasmota_t1/SENSOR/ENERGY/Power", "W", "Tasmota T1 Radiateur bureau"    , nextcolor(), "solid", 1, {"visible":False}, {} ),


    ( "pv/meter/req_time",   "s", "SDM630 req time"   , nextcolor(), "solid", 1, {"visible":False}, {} ),
    ( "pv/meter/req_period", "s", "SDM630 req period" , nextcolor(), "solid", 1, {"visible":False}, {} ),

    ( "pv/solis%d/meter/req_time",   "s", "SDM120 %s req time"   , nextcolor(), "solid", 1, {"visible":False}, {} ),
    ( "pv/solis%d/meter/req_period", "s", "SDM120 %s req period" , nextcolor(), "solid", 1, {"visible":False}, {} ),


    # SQL
    # ( 
    # SQLtopic( 
    #     """
    #     select toStartOfMinute(ts), max(kWh) from (
    #         select ts, sum(value*duration/3600000000) over (order by ts) kWh from (
    #             select ts, lagInFrame(value) over (order by ts) value, age('ms', lagInFrame(ts,1) over (order by ts), ts) duration 
    #             from mqtt_float where topic='pv/meter/total_power' and ts > '2024-10-01 00:00:00' order by ts
    #         ) where value>0
    #     ) group by 1 order by 1;
    #     """
    # ), "kWh" , "Grid import"                        , "#FF0000"       , "solid",    1.0 , {"visible":False}  , {} ),


]

#
#   [ [ "Tab name", [ list of rows [ list of traces ]]
PLOT_LAYOUTS = [
    [ 
        "PV", 
        [
            [
                "pv/total_pv_power"                              ,
                "pv/meter/house_power"                           ,
                "pv/meter/total_power"                           ,
                "pv/bms/power" ,
                "pv/total_grid_port_power"                       ,
                "pv/evse/meter/active_power"                     ,
            ],[
                "pv/bms/soc"                                 ,
            ]
        ]
    ],[ 
        "Route", 
        [
            [
                "pv/total_pv_power"                              ,
                "pv/meter/house_power"                           ,
                "pv/meter/total_power"                           ,
                "pv/total_input_power"                           ,
                "pv/total_battery_power"                           ,
                "pv/total_grid_port_power"                       ,
                # "pv/battery_max_charge_power"                           ,
                # "pv/bms/max_charge_power"                        ,
                # "pv/router/battery_min_charge_power"             ,
                "pv/evse/meter/active_power"                     ,
                "pv/evse/rwr_current_limit"                      ,

"tele/plugs/tasmota_t4/SENSOR/ENERGY/Power",
"tele/plugs/tasmota_t2/SENSOR/ENERGY/Power",
"tele/plugs/tasmota_t1/SENSOR/ENERGY/Power",
            ],
            [
                "cmnd/plugs/tasmota_t4/Power", 
                "cmnd/plugs/tasmota_t2/Power", 
                "cmnd/plugs/tasmota_t1/Power", 
                "pv/router/mppts_in_drop",
                "pv/router/evse/state",
            ]
        ]
    ],[ "Strings", 
        [
            [
                "pv/total_pv_power"                              ,
                "pv/solis%d/pv_power"                            ,
            ],[
                "pv/solis1/mppt1_power"                          ,
                "pv/solis1/mppt2_power"                          ,
                "pv/solis2/mppt1_power"                          ,
                "pv/solis2/mppt2_power"                          ,
            ],[
                "pv/solis%d/mppt1_voltage"                       ,
                "pv/solis%d/mppt2_voltage"                       ,
            ],[
                "pv/solis%d/mppt1_current"                       ,
                "pv/solis%d/mppt2_current"                       ,
            ]

        ]
    ],[ "Balance", 
        [
            [
                "pv/total_pv_power"                              ,
                "pv/meter/house_power"                           ,
                "pv/meter/total_power"                           ,
                "pv/total_battery_power"                         ,
            ],[
                "pv/meter/phase_1_power"                         ,
                "pv/meter/phase_2_power"                         ,
                "pv/meter/phase_3_power"                         ,
            ],[
                "pv/solis%d/meter/active_power"                  ,
                "pv/solis%d/battery_power"                         ,
                "pv/solis%d/pv_power"                            ,
            ]
        ]
    ],[ "Grid",
        [
            [
                "pv/meter/phase_1_line_to_neutral_volts",
                "pv/meter/phase_2_line_to_neutral_volts",
                "pv/meter/phase_3_line_to_neutral_volts",
            ],[
                "pv/meter/phase_1_current",
                "pv/meter/phase_2_current",
                "pv/meter/phase_3_current",
            ],[
                "pv/meter/frequency",
            ]
        ]
    ],[ "Energy", 
        [
            [
                "pv/evse/meter/import_active_energy",
                "pv/evse/energy",
                "pv/solis%d/meter/import_active_energy",
                "pv/solis%d/meter/export_active_energy",
                "pv/energy_generated_today",
                "pv/battery_charge_energy_today",
            ]
        ]
    ],[ "Machine",
        [
            [
                "pv/solis%d/temperature" ,
                "pv/cpu_temp_c"          ,
                "pv/cpu_load_percent"    ,
                "pv/disk_space_gb"       ,
                "pv/bms/temperature"     ,
                "pv/solis%d/fan_rpm"     ,
            ]
        ]
    ],["S1 history", 
        [
            [
                "pv/total_pv_power"                              ,
                "pv/meter/house_power"                           ,
                "pv/meter/total_power"                           ,
                "pv/solis1/battery_power"                        ,
                "pv/solis1/meter/active_power"                   ,
            ],[
                "pv/solis1/bms_battery_soc"                      ,
                "pv/solis1/bms_battery_current"                  ,
            ]
        ]
    ],["Chauffage",
        [
            [
                "chauffage/depart"             ,
                "chauffage/retour"             ,
                "chauffage/pac_depart"         ,
                "chauffage/pac_retour"         ,
                "chauffage/debit"              ,
                "chauffage/pompe"              ,
                "chauffage/et_bureau"          ,
                "chauffage/et_pcbt_depart"     ,
                "chauffage/et_pcbt_retour"     ,
                "chauffage/et_pcbt_ambient"    ,
                "chauffage/ext_parking"        ,
                "chauffage/ext_sous_balcon"    ,
                "chauffage/rc_pc_cuisine"      ,
                "chauffage/rc_pc_pcbt_ambient" ,
                "chauffage/rc_pc_pcbt_depart"  ,
                "chauffage/rc_pc_pcbt_retour"  ,
                "chauffage/rc_pf_pcbt_ambient" ,
                "chauffage/rc_pf_pcbt_depart"  ,
                "chauffage/rc_pf_pcbt_retour"  ,
                "chauffage/pac_puits"          ,
                "chauffage/pac_rejet"          ,
                "chauffage/rc_pf_che"          ,
            ]
        ]
    ],
    ["TEST", 
        [
            [
                # "pv/solis%d/battery_current",
                # "pv/bms/current",
                # "pv/solis%d/reserved_33191",
                "pv/solis%d/fakemeter/active_power",
                "pv/meter/house_power",
                "pv/meter/total_power",
                "pv/solis%d/meter/active_power",
            ],[
                "pv/bms/protection",
                "pv/bms/alarm",
                "pv/bms/charge_enable",
                # "pv/bms/max_charge_current",
                "pv/solis%d/fakemeter/lag",
                "pv/meter/req_time",
                "pv/meter/req_period",
"pv/solis%d/meter/req_time",
"pv/solis%d/meter/req_period",
            ],
        ]
    ],
    ["MQTT",
        [
            [
                "$SYS/broker/load/messages/received/1min",
                "$SYS/broker/load/messages/sent/1min",
            ],[
                "$SYS/broker/load/bytes/received/1min",
                "$SYS/broker/load/bytes/sent/1min",
            ]
        ]
    ]
]

def insert_inverters_data_streams( l ):
    for topic, unit, label, color, dash, scale, bokeh_attrs, attrs in l:
        if isinstance( topic, str ) and "%d" in topic:
            for solis, _ in (1,"dashed"),(2,"dotted"):
                if c:=colors.get(color):
                    c = c[solis]
                elif callable(color):
                    c=color()
                else:
                    c = color
                yield topic%solis, unit, label%solis, c, dash, scale, bokeh_attrs, attrs
        else:
            if c:=colors.get(color):
                c = c[0]
            elif callable(color):
                c=color()
            else:
                c = color
            yield topic, unit, label, c, dash, scale, bokeh_attrs, attrs

def insert_inverters_plot_layout( l ):
    if isinstance( l, list ):
        r = []
        for child in l:
            for e in insert_inverters_plot_layout(child):
                r.append(e)
        yield r
    else:
        if "%d" in l:
            for solis in 1,2:
                yield l%solis
        else:
            yield l

DATA_STREAMS = list( insert_inverters_data_streams( DATA_STREAMS ) )
PLOT_LAYOUTS = list(insert_inverters_plot_layout( PLOT_LAYOUTS ))[0]

#############################################################
#
#       Database
#
#############################################################

def get_latest_value( topic ):
    # current value
    r = clickhouse.execute( "SELECT value FROM mqtt.mqtt_float WHERE topic=%(topic)s ORDER BY ts DESC LIMIT 1", {"topic":topic} )
    # first value of day
    b = clickhouse.execute( "SELECT value FROM mqtt.mqtt_float WHERE topic=%(topic)s AND ts < toStartOfDay(now()) ORDER BY ts DESC LIMIT 1", {"topic":topic} )
    if r and b:
        return [b[0][0], r[0][0]]
    else:
        return [0,0]

class DataStream( object ):
    def __init__( self, topic, unit, label, color, dash, scale, bokeh_attrs, attrs ):
        if unit:    label += " (%s)" % unit
        self.bokeh_attrs = dict(bokeh_attrs)
        self.bokeh_attrs["legend_label"] = self.label = label
        self.bokeh_attrs["line_color"]   = self.color = color
        self.bokeh_attrs["line_dash"]    = dash
        self.bokeh_attrs["line_width"]   = 3
        self.topic = topic
        self.unit  = unit
        self.scale = scale
        self.attrs = attrs
        agg = attrs.get("aggregate","avg")
        if agg in ("avg","min","max"):    # sql aggregate
            self.sel1 = "%s(f%s)" % (agg,agg)
            self.sel2 = "%s(value)"%agg
        self.load()

    # load at startup
    def load( self ):
        x,y = self.get( time.time()-STREAMING_LENGTH_MAX )
        self.x = collections.deque( x )
        self.y = collections.deque( y )
        self.purge_old()
        # if self.x:
            # print( "Loaded %d for %s : %s-%s" % (len(self.x), self.topic, self.x[0], self.x[-1]))

    # add value from MQTT (streaming mode only)
    def add( self, y ):
        y*=self.scale
        if f := self.attrs.get("func"):
            y = f(y)
        t = np.datetime64(datetime.datetime.now(), "ms")
        if self.x:
            self.x.append( t )
            self.y.append( self.y[-1] )
        self.x.append( t )
        self.y.append( y )
        self.purge_old()

    # delete old values (streaming mode only)
    def purge_old( self ):
        t = np.datetime64(datetime.datetime.now(), "ms")
        limit = t - np.timedelta64( STREAMING_LENGTH_MAX, 's' )
        while len(self.x) and self.x[0] < limit:
            x = self.x.popleft()
            self.y.popleft()

    def realtime( self ):
        if self.y:
            return self.y[-1]
        else:
            return 0


    # get values from database (non streaming mode only)
    def get( self, tstart, tend=None, lod_length=3600 ):
        # All times in database are in UTC. Clickhouse treats integer timestamps as UTC too,
        # and will accept DateTime between (UTC int timestamp) and (UTC int timestamp)
        if isinstance( tstart, datetime.datetime ):
            tstart = tstart.timestamp()
            if tend:
                tend = tend.timestamp()
        span = (tend or time.time())-tstart
        assert span>=0

        # dynamic level of detail
        lod = int(round( span/lod_length ) or 1)
        overscan = 60
        tstart -= TIME_SHIFT_S + overscan
        if tend:
            tend -= TIME_SHIFT_S - overscan

        args   = { "topic":self.topic, "tstart":tstart, "tend":tend, "lod":lod, "lodm":lod//60 }
        kwargs = { "columnar":True, "settings":{"use_numpy":True} }            
        if tend:    ts_cond = "ts BETWEEN toDateTime64(%(tstart)s,1) AND toDateTime64(%(tend)s,1)"
        else:       ts_cond = "ts > toDateTime64(%(tstart)s,1)"
        where = " WHERE topic=%(topic)s AND " + ts_cond + " "

        t=time.time()
        if lod >= 6000:  r = clickhouse.execute( "SELECT toStartOfInterval(ts, INTERVAL %(lod)d SECOND) tsi, "+self.sel1+" FROM mqtt.mqtt_100minute"+where+"GROUP BY tsi ORDER BY tsi", args, **kwargs )
        elif lod >= 600: r = clickhouse.execute( "SELECT toStartOfInterval(ts, INTERVAL %(lod)d SECOND) tsi, "+self.sel1+" FROM mqtt.mqtt_10minute" +where+"GROUP BY tsi ORDER BY tsi", args, **kwargs )
        elif lod >= 60:  r = clickhouse.execute( "SELECT toStartOfInterval(ts, INTERVAL %(lod)d SECOND) tsi, "+self.sel1+" FROM mqtt.mqtt_minute"   +where+"GROUP BY tsi ORDER BY tsi", args, **kwargs )
        elif lod > 1:    r = clickhouse.execute( "SELECT toStartOfInterval(ts, INTERVAL %(lod)d SECOND) tsi, "+self.sel2+" FROM mqtt.mqtt_float"    +where+"GROUP BY tsi ORDER BY tsi", args, **kwargs )
        else:            r = clickhouse.execute( "SELECT ts,value FROM mqtt.mqtt_float"+where+"ORDER BY topic,ts", args, **kwargs )

        # if not r:  # no data, get latest available row
            # r = clickhouse.execute( "SELECT ts, value FROM mqtt.mqtt_float WHERE topic=%(topic)s ORDER BY ts DESC LIMIT 1", args, **kwargs )                
            
        if r:
            x,y = r
            y = np.array(y)*self.scale
            x += TIME_SHIFT
            if len(x) < 500:    # improve visibility of steps by adding points
                dt = np.timedelta64( 1, 'ms' )
                x=np.repeat(x,2)
                y=np.repeat(y,2)
                x[1:-1:2] = x[2::2] - dt

            if func:=self.attrs.get("func"):
                y = func(y)
            # print( "LOD: %s LEN %s %.01f ms %s" % (lod,len(x),(time.time()-t)*1000,self.topic))
            if self.attrs.get("mode") == "delta":
                y -= y[0]
            return x, y
        else:
            return ((),())

#   Server class, instantiated once
#
class BokehApp():
    def __init__(self):
        #
        #   Setup DataStreams
        self.data_streams = {}
        for args in DATA_STREAMS:
            # print( args )
            topic, unit, label, color, dash, scale, bokeh_attrs, attrs = args
            self.data_streams[ topic ] = DataStream( *args )

        io_loop = IOLoop.current()
        self.server = Server(applications = {'/myapp': Application(FunctionHandler(self.make_document))}, io_loop = io_loop, port = 5001)
        self.server.start()
        # self.server.show('/myapp')

        self.mqtt = MQTTClient( "bokehplot" )
        self.mqtt.on_connect    = self.mqtt_on_connect
        self.mqtt.on_message    = self.mqtt_on_message
        self.mqtt.set_auth_credentials( config.MQTT_USER, config.MQTT_PASSWORD )

        io_loop.add_callback(self.astart)
        io_loop.start()

    async def astart( self ):
        await self.mqtt.connect( config.MQTT_BROKER )
        print("MQTT Connected")

    def mqtt_on_connect(self, client, flags, rc, properties):
        print("MQTT On Connect")
        for topic in self.data_streams.keys():
            # print( "subscribe", topic )
            self.mqtt.subscribe( topic, qos=0 )

    def mqtt_on_message(self, client, topic, payload, qos, properties):
        if p := self.data_streams.get(topic):
            p.add( float(payload) )

    # When a Bokeh server session is initiated, the Bokeh server asks the Application for a new Document to service the session. 
    # To do this, the Application first creates a new empty Document, then it passes this new Document to the modify_document method of each of its handlers.
    # When all handlers have updated the Document, it is used to service the user session.
    def make_document(self, doc):
        PVDashboard(self, doc)

class PlotHolder():
    pass

#   One dashboard instance
#
class PVDashboard():
    def __init__( self, app, doc ):
        doc.theme = 'dark_minimal'
        self.app = app
        self.streaming_length = STREAMING_LENGTH

        # Duplicate Data Streams
        #   [ [ "Tab name", [ list of rows [ list of traces ]]
        print("Open dashboard", __name__)
        self.lines = {}
        self.figs  = []
        tabs = []
        for tab_title, tab_rows in PLOT_LAYOUTS:
            print( "Tab:", tab_title )
            rows = []
            for tab_row in tab_rows:
                print("    Row:")
                fig = bokeh.plotting.figure(   
                    # title = 'PV', 
                    sizing_mode   = "stretch_both", 
                    x_axis_type   = "datetime",
                    tools         = "undo,redo,reset,save,hover,box_zoom,xwheel_zoom,xpan,wheel_zoom,pan",
                    active_drag   = "xpan",
                    active_scroll = "xwheel_zoom"
                )
                if len(tabs) in (0,1) and len(rows)==1:
                    fig.sizing_mode = "stretch_width"
                    fig.height_policy = "fixed"
                    fig.height = 200

                for topic in tab_row:
                    p = PlotHolder()
                    p.tab    = len(tabs)
                    p.fig    = fig
                    p.topic  = topic
                    p.stream = stream = app.data_streams[ topic ]
                    p.line = fig.line( [],[], **stream.bokeh_attrs )
                    p.last_update = 0
                    print("         %20s %s" % (stream.label, topic))
                    self.lines.setdefault(topic,[]).append( p )

                fig.xaxis.ticker.desired_num_ticks = 20
                fig.legend.location = "top_left"
                fig.legend.click_policy="hide"

                fig.on_event( bokeh.events.Reset,        self.event_reset )
                fig.on_event( bokeh.events.LODStart,     self.event_lod_start )
                fig.on_event( bokeh.events.LODEnd,       self.event_lod_end )
                fig.on_event( bokeh.events.RangesUpdate, self.event_ranges_update )
                # fig.on_event( bokeh.events.MouseWheel,   self.event_generic )
                # fig.on_event( bokeh.events.Pan,          self.event_generic )

                fig.lod_factor    = 10
                fig.lod_interval  = 100
                fig.lod_threshold = None
                fig.lod_timeout   = 1000

                # link all X axes
                if self.figs:
                    fig.x_range = self.figs[0].x_range

                rows.append( fig )
                self.figs.append( fig )

            column = bokeh.layouts.column( *rows, width_policy="max", height_policy="max" )
            tabs.append( TabPanel( child=column, title=tab_title ))

        #
        #   Power Gauges
        #
        power_gauge_topics = (
                "pv/total_pv_power"                              ,
                "pv/total_input_power"                           ,
                "pv/meter/house_power"                           ,
                "pv/evse/meter/active_power"                     ,
                "pv/meter/total_power"                           ,
            )
        self.gauge_tab_power_gauge_datastreams = [ self.app.data_streams[k] for k in power_gauge_topics ]
        power_gauge_labels = [ ds.label for ds in self.gauge_tab_power_gauge_datastreams ]

        # Gauge tab
        p = self.power_gauge_fig = bokeh.plotting.figure( y_range=bokeh.models.FactorRange(factors=list(reversed(power_gauge_labels))), sizing_mode   = "stretch_both", x_range=(-5000,12000), title="Realtime", toolbar_location=None)
        tabs.append( TabPanel( child=self.power_gauge_fig, title="Gauges" ))
        self.gauge_tab_power_gauges = p.hbar( 
            y           =power_gauge_labels, 
            left        = [ ds.realtime() for ds in self.gauge_tab_power_gauge_datastreams ], 
            right       = [ ds.realtime() for ds in self.gauge_tab_power_gauge_datastreams ], 
            fill_color  = [ ds.bokeh_attrs["line_color"] for ds in self.gauge_tab_power_gauge_datastreams ],
            height      = 0.9, 
            line_width  = 0)
        # p.y_range.range_padding = 0.1
        # p.ygrid.grid_line_color = None
        # p.legend.location = "top_left"
        # p.axis.minor_tick_line_color = None
        # p.outline_line_color = None

        self.tabs = Tabs( width_policy="max", height_policy="max", tabs=tabs )
        self.tabs.on_change('active', self.event_on_tab_change )

        self.autorange_enabled = True
        self.button_group_actions_labels = [ "Autorange" ]
        self.button_group_actions = bokeh.models.widgets.CheckboxButtonGroup( labels=self.button_group_actions_labels, active=[0] )
        self.button_group_actions.on_change( "active", self.button_action_onclick )

        self.button_group_length_labels = [ "60min", "20min", "10min", "3min", "1min" ]
        self.button_group_length = bokeh.models.widgets.RadioButtonGroup( labels=self.button_group_length_labels, active=1 )
        self.button_group_length.on_change( "active", self.buttons_length_onclick )

        doc.add_root( bokeh.layouts.column( [ self.tabs, bokeh.layouts.row([ self.button_group_actions, self.button_group_length ]) ], width_policy="max", height_policy="max" ))

        self.lod_slider_value = 1

        self.lod_reduce = False
        self.streaming  = True
        self.tick = Metronome( 0.5 )
        self.prev_trange = None
        # self.update_title()
        self.event_reset()

        doc.add_periodic_callback( self.streaming_update, 500 )
        # doc.add_periodic_callback( self.update_title, 10000 )

    # def update_title( self ):
    #     for topic in TITLE_DATA.keys():
    #         TITLE_DATA[topic] = get_latest_value( topic )
    #     self.figs[0].title.text = "PV: Batt %d%% Import %.03f Export %.03f Total %.03f kWh" % (
    #             TITLE_DATA["pv/solis1/bms_battery_soc"][1],
    #             TITLE_DATA["pv/meter/total_import_kwh"][1]-TITLE_DATA["pv/meter/total_import_kwh"][0],
    #             TITLE_DATA["pv/meter/total_export_kwh"][1]-TITLE_DATA["pv/meter/total_export_kwh"][0],
    #             TITLE_DATA["pv/meter/total_import_kwh"][1]-TITLE_DATA["pv/meter/total_import_kwh"][0]-(TITLE_DATA["pv/meter/total_export_kwh"][1]-TITLE_DATA["pv/meter/total_export_kwh"][0])
    #         )

    def event_reset( self, event=None ):
        self.streaming = True
        self.figs[0].x_range.follow = "end"
        self.figs[0].x_range.follow_interval = np.timedelta64( self.streaming_length, 's' )
        self.figs[0].x_range.range_padding = 0
        self.streaming_update( force=True )

    #   Yields a list of PlotHolder's to redraw, removing those that don't need to be
    #   like inactive tabs, not visible, no data, etc
    def get_redraw_list( self, force=False ):
        for topic,plotholders in self.lines.items():
            for ph in plotholders:
                if force:
                    yield ph
                    continue
                stream  = ph.stream
                if self.tabs.active != ph.tab:      # ignore inactive tabs
                    continue
                if not ph.line.visible:
                    continue
                if self.streaming:
                    if not stream.x:
                        # if ph.line.data_source and len(ph.line.data_source["x"]) and not stream.x:
                            # last bit of data was evicted from the stream, but it is still displayed
                            # so we need to update it
                            # yield ph
                        continue
                    # if ph.last_update == stream.x[-1]:   # is there new data?
                        # continue                    # no new data, so no need to redraw
                yield ph

    def update_gauges( self ):
        ds = self.gauge_tab_power_gauges.data_source
        prev = None
        # ds.data["left"] = list(ds.data["right"])
        for n, datastream in enumerate( self.gauge_tab_power_gauge_datastreams ):
            if prev is None or n>=4:
                a = 0
                b = prev = datastream.realtime()
            else:
                a = prev
                b = prev = prev - datastream.realtime()
            prev = None
            ds.data["right"][n] = max( a,b )
            ds.data["left"] [n] = min( a,b )

        # First one is total_pv
        # ds.data["right"] = [ ds.realtime() for ds in self.gauge_tab_power_gauge_datastreams ]
        ds.trigger("data",ds.data,ds.data)

    def streaming_update( self, force=False ):
        self.update_gauges()
        if not self.streaming:
            return

        now = np.datetime64(datetime.datetime.now(), "ms")
        for ph in self.get_redraw_list():
            ds = ph.line.data_source
            stream  = ph.stream
            if stream.x[-1] == ph.last_update:  # nothing new to add
                continue
            last_update = ph.last_update
            ph.last_update = stream.x[-1]

            x = np.array( stream.x )
            y = np.array( stream.y )
            # x = np.hstack((x, [now]   ))      # add last point to finish line on right edge of screen
            # y = np.hstack((y, [y[-1]] ))

            trange = self.get_t_range()
            if trange:
                # do not send the whole history, only what will be displayed
                start_idx = np.searchsorted( x, np.datetime64(trange[0], "ms") )
                start_idx = max( 0, start_idx-1 )
                x = x[start_idx:]                   
                y = y[start_idx:]

                # if (not len(ds.data["x"])) or ds.data["x"][0] > x[0]:
                    # send enough data to fill screen
                ds.data = {"x":x, "y":y}
                ds.trigger('data', ds.data, ds.data )
                # else:
                #     # stream updates
                #     prev_idx = np.searchsorted( x, ds.data["x"][-1] , side="right" )
                #     if prev_idx < len(x) and  ds.data["x"][-1] >= x[ prev_idx ]:
                #         for px, py in zip( ds.data["x"][-3:], ds.data["y"][-3:] ):
                #             print( stream.topic, "old", px, py )
                #         for px, py in zip( x[prev_idx:], y[prev_idx:] ):
                #             print( stream.topic, "new", px, py )
                #         print()
                #     ds.stream( {"x":x[prev_idx:], "y":y[prev_idx:] } )#, rollover=len(x) )
                #     remove_old = len(ds.data["x"]) - len(x)
                #     if remove_old > 0:
                #         s = slice(remove_old)
                #         ds.patch ( {"x" : [(s, [])], "y" : [(s, [])] })
                #     print( stream.topic, "oldest", ds.data["x"][0], "len", len(ds.data["x"]) )
            else:
                ds.data = {"x":x, "y":y}
                ds.trigger('data', ds.data, ds.data )

    def get_t_range( self ):
        fig = self.figs[0]
        t1 = fig.x_range.start
        t2 = fig.x_range.end
        if np.isnan(t1) or np.isnan(t2):
            return None
        return int(t1), int(t2)  # in milliseconds

    def event_lod_start( self, event ):
        # print( event )
        self.streaming  = False
        # self.lod_reduce = True
        self.redraw( event )

    def event_lod_end( self, event ):
        # print( event )
        self.lod_reduce = False
        self.redraw( event, True )

    def event_ranges_update( self, event ):
        self.redraw( event )

    def redraw( self, event, force=False ):
        if self.streaming:
            self.streaming_update( force )
            return
        if not (trange := self.get_t_range()):
            return
        trange_change = trange != self.prev_trange
        self.prev_trange = trange
        if not (force or trange_change and self.tick.ticked()):
            return

        tstart = int(trange[0]) * 0.001
        tend   = int(trange[1]) * 0.001
        lod_length=300 if self.lod_reduce else int(5000 * 10/(10+self.lod_slider_value))
        # print( tstart, tend, event, self.lod_reduce, lod_length )

        updated = 0
        for ph in self.get_redraw_list():
            ds = ph.line.data_source
            stream  = ph.stream
            x,y = ph.stream.get( tstart, tend, lod_length=lod_length )
            ds.data = {"x":x, "y":y}
            ds.trigger('data', ds.data, ds.data )
            updated += 1
        # print("updated:", updated)

        self.autorange()
        self.tick.ticked()

    def event_on_tab_change( self, attr, old, new ):
        self.redraw( None, force=True )

    def autorange( self ):
        if not self.autorange_enabled:
            return
        minmax = {}
        for ph in self.get_redraw_list():
                ds = ph.line.data_source
                y = ds.data["y"]
                if len(y):
                    if ph.fig not in minmax:
                        minmax[ph.fig] = [y.min(),y.max()]
                    else:
                        minmax[ph.fig] = [min(minmax[ph.fig][0],y.min()),max(minmax[ph.fig][1],y.max())]
        for fig, (mi, ma) in minmax.items():
            fig.y_range.start = mi
            fig.y_range.end   = ma

    def button_action_onclick( self, attr, old_idx, new_idx ):
        old = [ self.button_group_actions_labels[_] for _ in old_idx ]
        new = [ self.button_group_actions_labels[_] for _ in new_idx ]
        clicked = set(new).symmetric_difference(old)
        enabled  = set(new).difference(old)

        # autorange button
        self.autorange_enabled = "Autorange" in enabled
        if "Autorange" in enabled:
            self.autorange()

    def buttons_length_onclick( self, attr, old_idx, new_idx ):
        old = self.button_group_length_labels[ old_idx ]
        new = self.button_group_length_labels[ new_idx ]
        if m:=re.match(r"(\d+)min",new):
            length = int(m.groups()[0])
            self.streaming_length = length*60
            print( self.streaming_length )
            self.event_reset()

    # def event_generic( self, event ):
        # self.redraw( event, force=True )


print("Start")
if __name__ == '__main__':
    app = BokehApp()


