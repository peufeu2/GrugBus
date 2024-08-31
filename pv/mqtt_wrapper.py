#!/usr/bin/python
# -*- coding: utf-8 -*-

import time, gmqtt, logging
import config

#
#       Wrapper around gmqtt, the fastesr MQTT client for python.
#       aiomqtt/paho is  too slow for the cheap cpu
#

log = logging.getLogger(__name__)

class MQTTWrapper:
    def __init__( self, identifier ):
        self.mqtt = gmqtt.Client( identifier )
        self.mqtt.on_connect    = self.on_connect
        self.mqtt.on_message    = self.on_message
        self.mqtt.on_disconnect = self.on_disconnect
        self.mqtt.on_subscribe  = self.on_subscribe
        self.mqtt.set_auth_credentials( config.MQTT_USER, config.MQTT_PASSWORD )
        self.published_data = {}

    # multi-publish
    # this is not declared async, and gmqtt publish() is not async either.
    # all it does is push messages to be published into a queue, then transmits
    # them in the background.
    def publish( self, prefix, data ):
        if not self.mqtt.is_connected:
            log.error( "Trying to publish %s on unconnected MQTT" % prefix )
            return

        t = time.monotonic()
        to_publish = {}

        #   do not publish duplicate data
        for k,v in data.items():
            k = prefix+k
            if p := self.published_data.get(k):
                if p[0] == v and t<p[1]:
                    continue
            self.published_data[k] = v,t+60 # set timeout to only publish constant data every N seconds
            to_publish[k] = v

        for k,v in to_publish.items():
            self.mqtt.publish( k, str(v), qos=0 )

    def on_connect(self, client, flags, rc, properties):
        pass

    def on_disconnect(self, client, packet, exc=None):
        pass

    async def on_message(self, client, topic, payload, qos, properties):
        pass

    def on_subscribe(self, client, mid, qos, properties):
        pass
