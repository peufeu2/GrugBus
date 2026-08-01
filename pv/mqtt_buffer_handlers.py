#!/usr/bin/python
# -*- coding: utf-8 -*-

import asyncio, os, time, orjson, datetime, functools, re

import config
from misc import *

"""
    This takes selected input topics and:
        - republishes them in a more useful format (one float or int per message) for consumption by
        bokeyplot real-time and storage in clickhouse
        - also stores them in clickhouse

    Stored but not republished:
        Existing topic trees (pv/...) are not republished.

    Republished and stored:
        ha_ss/domain/entity/state   -> ha/device/entity
        ha_ss/th_sejour_temperature -> ha/th_sejour/temperature

        z2m/device  json payload    -> ha/device/entity with float/int payload
        z2m/th_sejour               -> ha/th_sejour/temperature

"""

HA_REPUBLISH_TOPIC = "ha/"

#   helpers
#
STATE_TO_INT = { b"on": 1, "on": 1, b"off": 0, "off":0 }
def state_to_int( s ):
    return STATE_TO_INT.get( s.lower() )

#
#   Get message handler for topic (result is cached)
#
@functools.cache
def get_handler( topic ):
    topic_parts = topic.split("/")
    if h := ROOT_HANDLER.get( topic_parts[0] ):
        return h( topic, topic_parts[1:] )

    # when None is returned, message will not be handled and not logged

###############################################################################
#
#   Default handler: simply forward the data unchanged, and do not republish
#
###############################################################################

def get_default_handler( topic, topic_parts ):
    return default_handler

def default_handler( self, client, topic, payload, qos, properties ):
    yield False, topic, payload.decode()

###############################################################################
#
#   Z2M Handlers
#
###############################################################################

def get_z2m_handler( topic, topic_parts ):
    # skip availability, button action, only keep state
    if len( topic_parts ) != 1:
        return
    device = topic_parts[0]

    # handle devices like th12_
    if m:= re.match( r"^([a-z]+)\d+_?", device ):
        if h:= Z2M_HANDLERS.get( m.groups()[0] ):
            pub_topic = HA_REPUBLISH_TOPIC + device
            # throw away msg_topic since the result of this function is cached with
            # msg_topic, it will only be called with this one anyway
            return lambda self, client, msg_topic, payload, qos, properties: h( self, payload, pub_topic )

def z2m_cf_handler( self, payload, topic ): 
    d = orjson.loads( payload )
    yield True, topic+"/battery", int(d["battery"])
    yield True, topic+"/contact", int(d["contact"])

def z2m_df_handler( self, payload, topic ): 
    d = orjson.loads( payload )
    yield True, topic+"/battery"               , int(d["battery"])
    yield True, topic+"/smoke"                 , int(d["smoke"])
    yield True, topic+"/smoke_concentration"   , int(d["smoke_concentration"])

def z2m_dm_handler( self, payload, topic ): 
    d = orjson.loads( payload )
    yield True, topic+"/battery"               , int(d["battery"])
    yield True, topic+"/occupancy"             , int(d["occupancy"])

def z2m_switch_handler ( self, payload, topic ): 
    d = orjson.loads( payload )
    yield True, topic+"/state", state_to_int(d["state"])
    if "energy" in d:   yield True, topic+"/energy" , round( float(d["energy"]), 3 )
    if "power" in d:    yield True, topic+"/power"  , round( float(d["power"]), 1 )

def z2m_th_handler( self, payload, topic ): 
    d = orjson.loads( payload )
    if "battery" in d:
        yield True, topic+"/battery"     , int(d["battery"])
    elif "battery_state" in d:
        yield True, topic+"/battery"     , BATTERY_STATES.get(d["battery_state"],0)

    yield True, topic+"/humidity"    , round( float(d["humidity"]), 2 )
    yield True, topic+"/temperature" , round( float(d["temperature"]), 2 )

Z2M_HANDLERS = {
    "cf" : z2m_cf_handler,       
    "df" : z2m_df_handler,       
    "dm" : z2m_dm_handler,       
    "p"  : z2m_switch_handler,   
    "r"  : z2m_switch_handler,   
    "th" : z2m_th_handler,       
}
###############################################################################
#
#   HA StateStream Handlers
#
#   Currently only used for HA-only sensors not available in Z2M
#
###############################################################################
UNDEFINED = set((b"undefined",b"unavailable",b"unknown"))
def get_ha_statestream_handler( _topic, topic_parts ):
    if topic_parts[-1] == "state":
        domain = topic_parts[0]
        entity = topic_parts[1]

        # th_ computed sensors
        if domain == "sensor" and (m:=re.match( r"^(th_.+?)_([^_]+)$", entity )):
                pub_topic = HA_REPUBLISH_TOPIC + "/".join(m.groups())
                def ha_th_handler( self, client, msg_topic, payload, qos, properties ):
                    if payload not in UNDEFINED:
                        yield True, pub_topic, round( float( orjson.loads( payload ) ), 2 )
                return ha_th_handler

        # tasmota power, energy and state
        if m:=re.match( r"^(tasmota_t\d+)_?(.*)", entity ):
            tasmota, varname = m.groups()
            varname = {"energy_power":"power", "energy_total":"energy"}.get( varname )
            if domain == "sensor" and varname:
                pub_topic = f"{HA_REPUBLISH_TOPIC}{tasmota}/{varname}"
                def ha_tasmota_handler( self, client, msg_topic, payload, qos, properties ):
                    if payload not in UNDEFINED:
                        yield True, pub_topic, round( float( orjson.loads( payload ) ), 2 )
                return ha_tasmota_handler

            elif domain == "switch":
                pub_topic = f"{HA_REPUBLISH_TOPIC}{tasmota}/state"
                def ha_switch_handler( self, client, msg_topic, payload, qos, properties ):
                    yield True, pub_topic, state_to_int( payload.decode() )
                return ha_switch_handler

###############################################################################

   # Move HA SS to ha_ss topic in root

###############################################################################

ROOT_HANDLER = {
    "pv"         : get_default_handler,
    "chauffage"  : get_default_handler,
    "chauffage2" : get_default_handler,
    "$SYS"       : get_default_handler,
    "ha"         : get_default_handler,

    "z2m"        : get_z2m_handler,
    "ha_ss"      : get_ha_statestream_handler,
}

BATTERY_STATES = {
    "high": 100,
    "medium": 50,
    "low": 25
}

LOGGED_TOPICS = set()
