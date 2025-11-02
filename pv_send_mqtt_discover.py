#!/usr/bin/python
# -*- coding: utf-8 -*-

import asyncio, os, time, traceback, collections, logging, sys, datetime, re, uvloop, orjson, yaml, pprint
import collections
from path import Path

import config
from misc import *

from asyncio import get_event_loop
from grugbus.devices import Solis_S5_EH1P_6K_2020_Extras
from pv.mqtt_wrapper import MQTTWrapper

mqtt = MQTTWrapper( __file__, clean_session=True )

#
#   Send MQTT discovery messages for Home Assistant
#
#

HA_DISCOVERY_TOPIC = "z2m_discovery"

class DiscoveryTest(  ):
    def __init__( self ):
        pass

    def start( self ):
        with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
            runner.run(self.astart())

    async def astart( self ):
        await mqtt.mqtt.connect( config.MQTT_BROKER )

        # Stuff to publish on MQTT
        to_publish = {}

        # add something to the list
        def add_message( device_id, entity_class, topic, config ):
            print( "Topic:", topic )
            pprint.pprint( config )
            s = orjson.dumps( config )
            to_publish.setdefault( device_id, [] ).append( (topic, s) )

        defaults = {
            "qos": 0,
            "retain": False,
        }

        #   Home Assistant wants the full device info in the first message about this device.
        #   Then next messages can have just the device id without the extra info.
        #   Keep a list of device id's for which we already sent the full info.
        created_devices = set()

        #   Build MQTT discovery message for an entity.
        #
        def entity( entity_class, device_id, entity_id, **kwargs ):
            if device_id not in created_devices:
                created_devices.add( device_id )    # On first message about this device, send the full info
                device_dict = {"name": device_id}   # ie, just the name.
            else:
                device_dict = {}

            if unit := kwargs.pop( "unit", None ):
                kwargs["unit_of_measurement"] = unit

            if (decimals:=kwargs.pop( "decimals", None )) != None:
                if decimals == 0:
                    kwargs["value_template"] = "{{ value | int }}"
                else:
                    kwargs["value_template"] = "{{ value | round(%d)}}" % decimals

            add_message( device_id, entity_class, topic=f"{HA_DISCOVERY_TOPIC}/{entity_class}/{device_id}_{entity_id}/config",  config = defaults | {
                "name": entity_id,
                "unique_id": f"{device_id}_{entity_id}",
                "device": device_dict | {"identifiers": [f"{device_id}"],},
                "state_topic"  : f"{device_id}/{entity_id}"
            } | kwargs )

            # time.sleep( 0.1 )

        #   Builds "sensor" entity
        #
        def sensor( device_id, entity_id, **kwargs ):
            entity( "sensor", device_id, entity_id, **kwargs )

        #   Builds "binary_sensor" entity
        #
        def binary_sensor( device_id, entity_id, **kwargs ):
            entity( "binary_sensor", device_id, entity_id, **kwargs )

        #   Builds "number" entity
        #   If "topic" parameter is present, adds "state_topic" and "command_topic"
        def number( device_id, entity_id, **kwargs ):
            if topic := kwargs.pop( "topic" ):
                kwargs["state_topic" ]   = f"{topic}/{entity_id}"
                kwargs["command_topic" ] = f"cmnd/{topic}/{entity_id}"
            kwargs.setdefault( "command_template", "{{value}}")
            entity( "number", device_id, entity_id, **kwargs )

        #################################################
        #           EV forced charge
        #################################################

        number( "pv_router_evse", "force_charge_minimum_A"  , topic = "pv/router/evse", unit="A"  , min=6, max=30  , step=3  , mode="slider", icon="mdi:flash" )
        number( "pv_router_evse", "force_charge_minimum_soc", topic = "pv/router/evse", unit="%"  , min=0, max=100 , step=10 , mode="slider", icon="mdi:battery-lock" )
        number( "pv_router_evse", "force_charge_until_kWh"  , topic = "pv/router/evse", unit="kWh", min=0, max=40  , step=5  , mode="slider", icon="mdi:fuel" )

        #################################################
        #           Charge
        #################################################

        # mode = "slider" or "box"
        number( "pv_router_evse", "stop_charge_after_kWh", topic = "pv/router/evse", unit="kWh", min=0, max=100, step=1, mode="box", icon="mdi:fuel" )
        binary_sensor( "pv_router_evse", "paused", icon="mdi:power" , state_topic="pv/router/evse/paused", payload_on="0", payload_off="1" )

        sensor( "pv_router_evse", "state"    , icon="mdi:state-machine" ,             state_topic="pv/router/evse/state", value_template="{{ ['Débranché', 'Branché', 'Démarrage', 'Charge', 'Finalisation', 'Terminé'][int(value)] }}" )
        sensor( "pv_router_evse", "power"    , unit="W"  , decimals=0, icon="mdi:lightning-bolt", state_class="measurement", device_class="power"   , state_topic="pv/evse/meter/active_power" )
        sensor( "pv_router_evse", "energy"   , unit="kWh", decimals=3, icon="mdi:battery-50"    , state_class="measurement", device_class="energy"  , state_topic="pv/evse/energy" )
        sensor( "pv_router_evse", "countdown", unit="s"  , decimals=0, icon="mdi:clock-outline" , state_class="measurement", device_class="duration", state_topic="nolog/pv/router/evse/countdown" )

        #################################################
        #           Router
        #################################################
    
        # Create home assistant select for the charger config
        device_id = "pv_router"
        entity_id = "active_config"
        topic = f"pv/router/{entity_id}"
        entity_class = "select"
        add_message( device_id, entity_class, f"{HA_DISCOVERY_TOPIC}/{entity_class}/{device_id}_{entity_id}/config",  defaults | {
            "name": "active_config",
            "unique_id": f"{device_id}_{entity_id}",
            "device": {
                "identifiers": [f"{device_id}"],
                "name": device_id,
            },

            "icon": "mdi:priority-high",
            "options": [ '["evse_off"]', '["evse_low"]','["evse_mid"]','["evse_high"]','["evse_max"]','["evse_pvmax"]' ],
            "state_topic"  : topic,
            "command_topic": f"cmnd/{topic}",
            "command_template"  : "{{value}}"
        } )
        sensor( "pv_router", "config_description", icon="mdi:note-text-outline", state_topic="nolog/pv/router/config_description" ) #, value_template='{{value_json.desc|replace("\n","<br>")}}' )

        #################################################
        #           PV information display
        #################################################

        sensor( "pv_pv",     "total_pv_power"               , unit="W"  , decimals=0, icon="mdi:white-balance-sunny"        , state_class="measurement", device_class="power", state_topic="pv/total_pv_power"     )
        sensor( "pv_pv",     "house_power"                  , unit="W"  , decimals=0, icon="mdi:home"                       , state_class="measurement", device_class="power", state_topic="pv/meter/house_power"  )
        sensor( "pv_pv",     "grid_power"                   , unit="W"  , decimals=0, icon="mdi:transmission-tower"         , state_class="measurement", device_class="power", state_topic="pv/meter/total_power"  )
        sensor( "pv_pv",     "battery_power"                , unit="W"  , decimals=0, icon="mdi:home-battery"               , state_class="measurement", device_class="power", state_topic="pv/bms/power"          )
        sensor( "pv_solis1", "mppt1_power"                  , unit="W"  , decimals=0, icon="mdi:solar-power-variant-outline", state_class="measurement", device_class="power", state_topic="pv/solis1/mppt1_power" )
        sensor( "pv_solis1", "mppt2_power"                  , unit="W"  , decimals=0, icon="mdi:solar-power-variant-outline", state_class="measurement", device_class="power", state_topic="pv/solis1/mppt2_power" )
        sensor( "pv_solis2", "mppt1_power"                  , unit="W"  , decimals=0, icon="mdi:solar-power-variant-outline", state_class="measurement", device_class="power", state_topic="pv/solis2/mppt1_power" )
        sensor( "pv_solis2", "mppt2_power"                  , unit="W"  , decimals=0, icon="mdi:solar-power-variant-outline", state_class="measurement", device_class="power", state_topic="pv/solis2/mppt2_power" )
        sensor( "pv_solis1", "meter_active_power"           , unit="W"  , decimals=0, icon="mdi:lightning-bolt"             , state_class="measurement", device_class="power", state_topic="pv/solis1/meter/active_power" )
        sensor( "pv_solis2", "meter_active_power"           , unit="W"  , decimals=0, icon="mdi:lightning-bolt"             , state_class="measurement", device_class="power", state_topic="pv/solis2/meter/active_power" )
        sensor( "pv_solis2", "backup_load_power"            , unit="W"  , decimals=1, icon="mdi:lightning-bolt"             , state_class="measurement", device_class="power", state_topic="pv/solis2/backup_load_power" )

        sensor( "pv_pv",     "battery_soc"                  , unit="%"  , decimals=0, icon="mdi:battery-50"                 , state_class="measurement"     , device_class="battery"    , state_topic="pv/bms/soc"            )
        sensor( "pv_pv",     "battery_soh"                  , unit="%"  , decimals=0, icon="mdi:bottle-tonic-plus"          , state_class="measurement"     , device_class="battery"    , state_topic="pv/bms/soh"            )
        sensor( "pv_pv",     "energy_generated_today"       , unit="kWh", decimals=3, icon="mdi:white-balance-sunny"        , state_class="total_increasing", device_class="energy"     , state_topic="pv/energy_generated_today" )
        sensor( "pv_pv",     "battery_charge_energy_today"  , unit="kWh", decimals=3, icon="mdi:home-battery"               , state_class="total_increasing", device_class="energy"     , state_topic="pv/battery_charge_energy_today" )
        sensor( "pv_pv",     "battery_temperature"          , unit="°C" , decimals=1, icon="mdi:thermometer"                , state_class="measurement"     , device_class="temperature", state_topic="pv/bms/temperature" )
        sensor( "pv_solis1", "temperature"                  , unit="°C" , decimals=1, icon="mdi:thermometer"                , state_class="measurement"     , device_class="temperature", state_topic="pv/solis1/temperature" )
        sensor( "pv_solis2", "temperature"                  , unit="°C" , decimals=1, icon="mdi:thermometer"                , state_class="measurement"     , device_class="temperature", state_topic="pv/solis2/temperature" )

        binary_sensor( "pv_solis1", "rwr_power_on_off", icon="mdi:power", state_topic="pv/solis1/rwr_power_on_off", payload_on=str(0xBE), payload_off=str(0xDE) )
        binary_sensor( "pv_solis2", "rwr_power_on_off", icon="mdi:power", state_topic="pv/solis2/rwr_power_on_off", payload_on=str(0xBE), payload_off=str(0xDE) )
        binary_sensor( "pv_solis2", "rwr_backup_output_enabled", icon="mdi:power", state_topic="pv/solis2/rwr_backup_output_enabled", payload_on="1", payload_off="0" )

        # publish grouped by device, otherwise home assistant bugs out
        print( f"""To remove retained messages, issue: \n mosquitto_sub -h host -u user -P password -t "{HA_DISCOVERY_TOPIC}/#" -v --remove-retained""")
        for device_id, messages in to_publish.items():
            print( "%20s: %d entities"%(device_id, len(messages)) )
            for topic, s in messages:
                # print( topic, s[:40] )
                # mqtt.mqtt.publish( topic, None, qos=1, retain=True )
                await asyncio.sleep( 0.001 )    # send queued messages
                # mqtt.mqtt.publish( topic, s, qos=0 )
                mqtt.mqtt.publish( topic, s, qos=1, retain=True, message_expiry_interval=3600*25 )
                await asyncio.sleep( 0.001 )    # send queued messages

        # ask router to publish settings
        mqtt.mqtt.publish( "cmnd/pv/router/evse/settings", "", qos=0 )
        mqtt.mqtt.publish( "cmnd/pv/router/settings", "", qos=0 )

        await mqtt.mqtt.disconnect()



if __name__ == '__main__':
    DiscoveryTest( ).start()