#!/usr/bin/python
# -*- coding: utf-8 -*-

import os, time, sys, serial, logging, logging.handlers, orjson
from path import Path

# This program is supposed to run on a potato (Allwinner H3 SoC) and uses async/await,
# so import the fast async library uvloop
import uvloop, asyncio
#aiohttp, aiohttp.web

# Modbus
import pymodbus
from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException
from asyncio.exceptions import TimeoutError, CancelledError

# Device wrappers and misc local libraries
from pv.mqtt_wrapper import MQTTWrapper, MQTTSetting, MQTTVariable
import grugbus
import pv.evse_abb_terra
import pv.reload, pv.router_coroutines
import config
from misc import *

###########################################################################################
#
#       This file starts and runs the PV router.
#       Not much interesting code here, it's all in the classes,
#       and pv/router.py which can be reloaded at runtime.
#
###########################################################################################

"""
    python3.11
    pymodbus 3.7.x

Reminder:
git remote set-url origin https://<token>@github.com/peufeu2/GrugBus.git
"""
# pymodbus.pymodbus_apply_logging_config( logging.DEBUG )
logging.basicConfig( encoding='utf-8', 
                     level=logging.INFO,
                     format='[%(asctime)s] %(levelname)s:%(message)s',
                     handlers=[
                            logging.handlers.RotatingFileHandler(Path(__file__).stem+'.log', mode='a', maxBytes=5*1024*1024, backupCount=2, encoding=None, delay=False),
                            # logging.FileHandler(filename=Path(__file__).stem+'.log'), 
                            logging.StreamHandler(stream=sys.stdout)
                    ])
log = logging.getLogger(__name__)


class Data:
    def __init__( self, mqtt, mqtt_topic ):
        self.mqtt = mqtt
        self.mqtt_topic = mqtt_topic

########################################################################################
#
#       Put it all together
#
########################################################################################
class Master():
    def __init__( self ):
        self.event_power = asyncio.Event()

    #
    #   Build hardware
    #
    async def astart( self ):
        self.mqtt = MQTTWrapper( "pv_master" )
        self.mqtt_topic = "pv/"
        await self.mqtt.mqtt.connect( config.MQTT_BROKER_LOCAL )

        # Get battery current from BMS
        # MQTTVariable( "pv/bms/current", self, "bms_current", float, None, 0 )
        # MQTTVariable( "pv/bms/power",   self, "bms_power",   float, None, 0 )
        MQTTVariable( "pv/bms/soc",     self, "bms_soc",     float, None, 0 )

        # Get information from Controller
        MQTTVariable( "nolog/pv/router_data" , self, "router_data" , orjson.loads, None, 0, self.mqtt_update_callback )

        #
        #   EVSE and its smartmeter, both on the same modbus port
        #
        modbus_evse = AsyncModbusSerialClient( **config.EVSE["SERIAL"] )
        self.evse  = pv.evse_abb_terra.EVSE( 
            modbus_evse,
            **config.EVSE["PARAMS"], 
            local_meter = pv.meters.SDM120( 
                modbus_evse,
                mqtt       = self.mqtt,
                mqtt_topic = "pv/evse/meter/" ,
                **config.EVSE["LOCAL_METER"]["PARAMS"], 
            ),
            mqtt        = self.mqtt, 
            mqtt_topic  = "pv/evse/" 
        )

        # add voltage to EVSE meter register poll list
        self.evse.local_meter.reg_sets[0].append( self.evse.local_meter.voltage )

        self.router = Router( mqtt = self.mqtt, mqtt_topic = "pv/router/" )

        pv.reload.add_module_to_reload( "config", self.mqtt.load_rate_limit ) # reload rate limit configuration

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task( self.log_coroutine( "Read: %s"             %self.evse.key, self.evse.read_coroutine() ))
                tg.create_task( self.log_coroutine( "Read: %s local meter" %self.evse.key, self.evse.local_meter.read_coroutine() ))
                tg.create_task( self.log_coroutine( "Reload python modules",     pv.reload.reload_coroutine() ))
                tg.create_task( pv.reload.reloadable_coroutine( "Router", lambda: pv.router_coroutines.route_coroutines, self ))

        except (KeyboardInterrupt, CancelledError):
            print("Terminated.")
        finally:
            await self.mqtt.mqtt.disconnect()
            with open("mqtt_stats/pv_master.txt","w") as f:
                self.mqtt.write_stats( f )

    ########################################################################################
    #
    #   Compute power values and fill fake meter fields
    #
    ########################################################################################

    async def mqtt_update_callback( self, param ):
        for k in (
            "m_total_power", "m_p1_v", "m_p2_v", "m_p3_v", "m_p1_i", "m_p2_i", "m_p3_i", 
            "meter_power_tweaked", "house_power", "total_pv_power", "total_input_power", 
            "total_grid_port_power", "total_battery_power", "battery_max_charge_power", 
            "data_timestamp"):
            setattr( self, k, param[k] )

        self.event_power.set()
        self.event_power.clear()

    #
    #   Async entry point
    #
    def start( self ):
        with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
            runner.run(self.astart())

    async def log_coroutine( self, title, fut ):
        log.info("Start:"+title )
        try:        await fut
        finally:    log.info("Exit: "+title )

if 1:
    try:
        mgr = Master()
        mgr.start()
    finally:
        logging.shutdown()
else:
    import cProfile
    with cProfile.Profile( time.process_time ) as pr:
        pr.enable()
        try:
            mgr = Master()
            mgr.start()
        finally:
            logging.shutdown()
            pr.dump_stats("profile.dump")






