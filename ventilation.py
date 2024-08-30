#!/usr/bin/python
# -*- coding: utf-8 -*-

import os, time, sys, traceback, asyncio, uvloop, logging
from path import Path

import config
from misc import *
from pv.mqtt_wrapper import MQTTWrapper

logging.basicConfig( #encoding='utf-8', 
                     level=logging.INFO,
                     format='[%(asctime)s] %(levelname)s:%(message)s',
                     handlers=[logging.FileHandler(filename=Path(__file__).stem+'.log'), 
                            logging.StreamHandler(stream=sys.stdout)])
log = logging.getLogger(__name__)

class ControleVentilation( MQTTWrapper ):
    def __init__( self ):
        super().__init__( "ventilation" )
        self.received_data = {}

    def on_connect( self, client, flags, rc, properties ):
        self.mqtt.subscribe( "chauffage/ext_sous_balcon" )
        self.mqtt.subscribe( "chauffage/rc_pf_pcbt_ambient" )
        self.mqtt.subscribe( "chauffage/et_pcbt_ambient" )

    async def on_message(self, client, topic, payload, qos, properties):
        self.received_data[topic] = payload

    def start( self ):
        with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
            runner.run(self.astart())

    async def astart( self ):
        fan_on_pf = 0
        fan_on_et = 0
        await self.mqtt.connect( config.MQTT_BROKER_LOCAL )
        self.publish( "cmnd/plugs/tasmota_t5/", {"Power": 0} )
        self.publish( "cmnd/plugs/tasmota_t6/", {"Power": 0} )
        try:
            while True:
                try:
                    await asyncio.sleep(10)

                    try:
                        temp_ext = float( self.received_data["chauffage/ext_sous_balcon"] )
                        temp_pf  = float( self.received_data["chauffage/rc_pf_pcbt_ambient"] )
                        temp_et  = float( self.received_data["chauffage/et_pcbt_ambient"] )
                    except:
                        traceback.print_exc()
                        print("Waiting for data...")
                        continue

                    fan_on_pf = int( (temp_pf + fan_on_pf*0.2) > (temp_ext) )
                    fan_on_et = int( (temp_et + fan_on_et*0.2) > (temp_ext) )

                    print( "et_pcbt_ambient    ", temp_et, "Fan:", fan_on_et )
                    print( "rc_pf_pcbt_ambient ", temp_pf, "Fan:", fan_on_pf)
                    print( "ext_sous_balcon    ", temp_ext )
                    print()

                    self.publish( "cmnd/plugs/tasmota_t5/", {"Power": fan_on_pf} )
                    self.publish( "cmnd/plugs/tasmota_t6/", {"Power": fan_on_et} )

                except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
                    print("Terminated.")
                    return
                except:
                    log.exception( "Exception" )
                    await asyncio.sleep(1)        
        finally:
            self.publish( "cmnd/plugs/tasmota_t5/", {"Power": 0} )
            self.publish( "cmnd/plugs/tasmota_t6/", {"Power": 0} )

mgr = ControleVentilation()
mgr.start()
