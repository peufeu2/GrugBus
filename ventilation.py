#!/usr/bin/python
# -*- coding: utf-8 -*-

import os, pprint, time, sys, serial, socket, traceback, struct, datetime, logging, math, traceback, shutil, collections
from path import Path


# This program is supposed to run on a potato (Allwinner H3 SoC) and uses async/await,
# so import the fast async library uvloop
import asyncio, signal, uvloop

# MQTT
from gmqtt import Client as MQTTClient

# Device wrappers and misc local libraries
from misc import *
import config

# pymodbus.pymodbus_apply_logging_config( logging.DEBUG )
logging.basicConfig( encoding='utf-8', 
                     level=logging.INFO,
                     format='[%(asctime)s] %(levelname)s:%(message)s',
                     handlers=[logging.FileHandler(filename=Path(__file__).stem+'.log'), 
                            logging.StreamHandler(stream=sys.stdout)])
log = logging.getLogger(__name__)


#
#   Housekeeping for async multitasking:
#   If one thread coroutine abort(), fire STOP event to allow program exit
#   and set STILL_ALIVE to False to stop all other threads.
#
STILL_ALIVE = True
STOP = asyncio.Event()
def abort():
    STOP.set()
    global STILL_ALIVE
    STILL_ALIVE = False

# Helper to abort program when the coroutine passed as parameter exits
async def abort_on_exit( awaitable ):
    await awaitable
    log.info("*** Exited: %s", awaitable)
    return abort()

########################################################################################
#   MQTT
########################################################################################
class MQTT():
    def __init__( self, client_name ):
        self.mqtt = MQTTClient( client_name )
        self.mqtt.on_connect    = self.on_connect
        self.mqtt.on_message    = self.on_message
        self.mqtt.on_disconnect = self.on_disconnect
        self.mqtt.on_subscribe  = self.on_subscribe
        self.mqtt.set_auth_credentials( config.MQTT_USER, config.MQTT_PASSWORD )
        self.published_data = {}
        self.received_data  = {}

    def on_connect(self, client, flags, rc, properties):
        self.mqtt.subscribe('chauffage/#', qos=0)

    def on_disconnect(self, client, packet, exc=None):
        pass

    async def on_message(self, client, topic, payload, qos, properties):
        # print( "MQTT", topic, payload )
        self.received_data[ topic ] = payload

    def on_subscribe(self, client, mid, qos, properties):
        print('MQTT SUBSCRIBED')

    def publish( self, prefix, data, add_heartbeat=False ):
        if not self.mqtt.is_connected:
            log.error( "Trying to publish %s on unconnected MQTT" % prefix )
            return

        t = time.time()
        to_publish = {}
        if add_heartbeat:
            data["heartbeat"] = int(t) # do not publish more than 1 heartbeat per second

        #   do not publish duplicate data
        for k,v in data.items():
            k = prefix+k
            p = self.published_data.get(k)
            if p:
                if p[0] == v and t<p[1]:
                    continue
            self.published_data[k] = v,t+60 # set timeout to only publish constant data every N seconds
            to_publish[k] = v

        for k,v in to_publish.items():
            self.mqtt.publish( k, str(v), qos=0 )

        return True

mqtt = MQTT( "ventilation" )

class ControleVentilation( object ):
    def __init__( self ):
        pass

    def start( self ):
        if sys.version_info >= (3, 11):
            with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
                runner.run(self.astart())
        else:
            uvloop.install()
            asyncio.run(self.astart())

    async def astart( self ):
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGINT,  abort)
        loop.add_signal_handler(signal.SIGTERM, abort)
        asyncio.create_task( self.main_coroutine() )
        asyncio.create_task( self.mqtt_start() )

        await STOP.wait()
        await mqtt.mqtt.disconnect()

    async def mqtt_start( self ):
        await mqtt.mqtt.connect( config.MQTT_BROKER_LOCAL )

    async def main_coroutine( self ):
        fan_on_pf = 1
        fan_on_et = 1
        mqtt.publish( "cmnd/plugs/tasmota_t5/", {"Power": 0} )
        mqtt.publish( "cmnd/plugs/tasmota_t6/", {"Power": 0} )
        try:
            while STILL_ALIVE:
                try:
                    await asyncio.sleep(60)

                    temp_ext = float( mqtt.received_data["chauffage/ext_sous_balcon"] )
                    temp_pf  = float( mqtt.received_data["chauffage/rc_pf_pcbt_ambient"] )
                    temp_et  = float( mqtt.received_data["chauffage/et_pcbt_ambient"] )

                    fan_on_pf = int( (temp_pf + fan_on_pf*0.2) > (temp_ext) )
                    fan_on_et = int( (temp_et + fan_on_et*0.2) > (temp_ext) )

                    print( "et_pcbt_ambient    ", temp_et )
                    print( "rc_pf_pcbt_ambient ", temp_pf)
                    print( "ext_sous_balcon    ", temp_ext )
                    print( "fan_on_et          ", fan_on_et )
                    print( "fan_on_pf          ", fan_on_pf )
                    print()

                    mqtt.publish( "cmnd/plugs/tasmota_t5/", {"Power": fan_on_pf} )
                    mqtt.publish( "cmnd/plugs/tasmota_t6/", {"Power": fan_on_et} )

                except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
                    return abort()
                except:
                    log.error(traceback.format_exc())
        finally:
            mqtt.publish( "cmnd/plugs/tasmota_t5/", {"Power": 0} )
            mqtt.publish( "cmnd/plugs/tasmota_t6/", {"Power": 0} )





mgr = ControleVentilation()
try:
    mgr.start()
finally:
    logging.shutdown()
