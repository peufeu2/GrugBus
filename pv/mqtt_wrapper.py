#!/usr/bin/python
# -*- coding: utf-8 -*-

import time, gmqtt, logging
from misc import *
import config

#
#       Wrapper around gmqtt, the fastesr MQTT client for python.
#       aiomqtt/paho is  too slow for the cheap cpu
#

log = logging.getLogger(__name__)

class RateLimit:
    __slots__ = "text","value","margin","tick","sum","count","mode"
    def __init__( self, margin, period, mode, offset ):
        self.margin    = margin or 0
        self.tick      = Metronome(( period, offset ))
        self.text      = None
        self.value     = 0
        self.sum       = 0
        self.count     = 0
        self.mode      = mode

    def reset( self, value ):
        self.value = value
        self.sum = 0
        self.count = 0

    def add( self, value ):
        self.value = value
        self.sum += value
        self.count += 1

    def avg( self ):
        r = self.sum / (self.count or 1)
        self.sum = self.count = 0
        return r

class MQTTWrapper:
    def __init__( self, identifier ):
        self.mqtt = gmqtt.Client( identifier )
        self.mqtt.on_connect    = self.on_connect
        self.mqtt.on_message    = self.on_message
        self.mqtt.on_disconnect = self.on_disconnect
        self.mqtt.on_subscribe  = self.on_subscribe
        self.mqtt.set_auth_credentials( config.MQTT_USER, config.MQTT_PASSWORD )
        self.is_connected = False
        self._published_data = {}

        for topic, (period, margin, mode) in config.MQTT_RATE_LIMIT.items():
            p = self._published_data[topic] = RateLimit( margin, period, mode, len(self._published_data)%60 )

    def publish_reg( self, topic, reg ):
        self.publish_value( topic+reg.key, reg.value, reg._format_value )

    def publish_value( self, topic, value, format=str ):
        if not self.mqtt.is_connected:
            log.error( "Trying to publish %s on unconnected MQTT" % prefix )
            return

        if value is None:
            return

        # rate limit constant data
        if p := self._published_data.get(topic):
            if abs(value-p.value)>p.margin:
                self.mqtt.publish( topic, format(value), qos=0 )
                p.reset( value )
                return

            p.add( value )
            if p.tick.ticked():
                if p.mode == "avg":
                    self.mqtt.publish( topic, format(p.avg()), qos=0 )
                else:
                    self.mqtt.publish( topic, format(value), qos=0 )
                p.reset( value )
                return
        else:
            p = self._published_data[topic] = RateLimit( 0, 60, "", len(self._published_data)%60 )
            p.reset( value )
            log.info( "MQTT: No ratelimit for %s", topic )
            self.mqtt.publish( topic, format( value ), qos=0 )

    def publish( self, topic, text ):
        if not self.mqtt.is_connected:
            log.error( "Trying to publish %s on unconnected MQTT" % prefix )
            return

        # rate limit constant data
        if p := self._published_data.get(topic):
            if not p.tick.ticked():
                if text == p.text:
                    return
        else:
            p = self._published_data[topic] = RateLimit( 0, 60, "", len(self._published_data)%60 )
            log.info( "MQTT: No ratelimit for %s", topic )
        p.text      = text
        self.mqtt.publish( topic, text, qos=0 )

    def on_connect(self, client, flags, rc, properties):
        self.is_connected = True

    def on_disconnect(self, client, packet, exc=None):
        self.is_connected = False

    async def on_message(self, client, topic, payload, qos, properties):
        pass

    def on_subscribe(self, client, mid, qos, properties):
        pass

    # limit "topic" (and all descendants) to one message every "period" seconds
    # unless there is a change of more than 
    def rate_limit( self, topic, period, change ):
        self

