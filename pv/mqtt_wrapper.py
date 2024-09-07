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
    __slots__ = "text","value","margin","tick"

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

        for topic, (period, margin) in config.MQTT_RATE_LIMIT.items():
            p = self._published_data[topic] = RateLimit()
            p.margin    = margin or 0
            p.tick      = Metronome(( period, len(self._published_data)%60 ))
            p.text      = None
            p.value     = 0

    def publish_reg( self, topic, reg ):
        self.publish( topic+reg.key, reg.format_value(), reg.value )

    def publish_value( self, topic, value ):
        self.publish( topic, value, value )

    def publish( self, topic, text, value=None ):
        if not self.mqtt.is_connected:
            log.error( "Trying to publish %s on unconnected MQTT" % prefix )
            return

        # rate limit constant data
        if p := self._published_data.get(topic):
            if not p.tick.ticked():
                if text == p.text:
                    return
                if value is not None and abs(value-p.value)<=p.margin:
                    return
        else:
            p = self._published_data[topic] = RateLimit()
            p.margin    = -1
            p.tick      = Metronome(( 60, len(self._published_data)%60 ))
            log.info( "MQTT: No ratelimit for %s", topic )
        p.text      = text
        p.value     = value
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

