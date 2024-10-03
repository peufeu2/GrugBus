#!/usr/bin/python
# -*- coding: utf-8 -*-

import time, gmqtt, logging, functools
from path import Path
from misc import *
import config

#
#       Wrapper around gmqtt, the fastesr MQTT client for python.
#       aiomqtt/paho is  too slow for the cheap cpu
#

log = logging.getLogger(__name__)

class RateLimit:
    __slots__ = "text","value","last_pub","margin","start_time","period","sum","count","mode","total_count","published_count","is_constant"
    def __init__( self, margin, period, mode ):
        self.margin    = margin or 0
        self.start_time = time.monotonic()
        self.period    = period
        self.text      = None
        self.value     = 0
        self.last_pub    = 0
        self.sum       = 0
        self.count     = 0
        self.mode      = mode
        self.total_count = 0
        self.published_count = 0
        self.is_constant  = False

    def reset( self, value, last_pub=None ):
        self.value = value
        self.last_pub = last_pub or value
        self.sum = value
        self.count = 1
        self.is_constant = True
        self.start_time = time.monotonic()
        self.published_count += 1
        self.total_count += 1

    def add( self, value ):
        if self.count and value != self.value:
            self.is_constant = False
        self.sum += value
        self.count += 1
        self.total_count += 1

    def avg( self ):
        return self.sum / (self.count or 1)

class MQTTWrapper:
    _callbacks_generated = set()

    def __init__( self, identifier ):
        self.mqtt = gmqtt.Client( identifier, clean_session=False )
        self.mqtt.on_connect    = self.on_connect
        self.mqtt.on_message    = self.on_message
        self.mqtt.on_disconnect = self.on_disconnect
        self.mqtt.on_subscribe  = self.on_subscribe
        self.mqtt.set_auth_credentials( config.MQTT_USER, config.MQTT_PASSWORD )
        self.is_connected = False
        self._published_data = {}
        self._subscriptions = {}
        self._startup_time = time.monotonic()
        self.load_rate_limit()

    def load_rate_limit( self ):
        log.info("MQTT: Load rate limits")
        for topic, (period, margin, mode) in config.MQTT_RATE_LIMIT.items():
            self._published_data[topic] = RateLimit( margin, period, mode )

    def get_rate_limit( self, topic ):
        return RateLimit( *config.MQTT_RATE_LIMIT.get( topic, (60, 0, "") ))

    def write_stats( self, file ):
        maxlen = 1+max( len(topic) for topic in self._published_data.keys() )
        duration = time.monotonic() - self._startup_time
        for topic, p in sorted( self._published_data.items(), key=lambda kv: kv[1].total_count, reverse=True ):
            file.write( f"{topic!r:<{maxlen}}: ({p.period:4f}, {p.margin:>10.03f}, {p.mode!r:8s}), # {p.published_count/duration:6.03f}/{p.total_count/duration:6.03f},\n" )

    def publish_reg( self, topic, reg ):
        self.publish_value( topic+reg.key, reg.value, reg._format_value )

    #   Publish numeric value, rate limit when changes are small, average
    #
    def publish_value( self, topic, value, format=str, qos=0 ):
        if not self.mqtt.is_connected:
            log.error( "Trying to publish %s on unconnected MQTT" % topic )
            return

        if value is None:
            return

        # rate limit
        if p := self._published_data.get(topic):
            # If value moved more than p.margin, we must publish. 
            # Previous unpublished values within p.margin are discarded.
            if abs(value - p.last_pub)>p.margin:
                self.mqtt.publish( topic, format(value), qos=0 )
                p.reset( value )
                return

            p.add( value )  # add to average

            # value is still within p.margin.
            # Publish only on periodic interval
            if time.monotonic() < p.start_time + p.period:
                return

            # periodic interval elapsed, so publish it
            if p.mode == "avg":
                pub = p.avg()
                self.mqtt.publish( topic, format(pub), qos=0 )
                p.reset( value, pub )
            else:
                self.mqtt.publish( topic, format(value), qos=0 )
                p.reset( value )

        else:
            p = self._published_data[topic] = self.get_rate_limit( topic )
            p.reset( value )
            log.info( "MQTT: No ratelimit for %s", topic )
            self.mqtt.publish( topic, format( value ), qos=0 )

    #   Publish text value, rate limit
    #
    def publish( self, topic, text ):
        if not self.mqtt.is_connected:
            log.error( "Trying to publish %s on unconnected MQTT" % prefix )
            return

        # rate limit constant data
        if p := self._published_data.get(topic):
            p.total_count += 1
            if text == p.text and time.monotonic() < p.start_time + p.period:
                return
        else:
            p = self._published_data[topic] = self.get_rate_limit( topic )
            p.total_count = 1
            log.info( "MQTT: No ratelimit for %s", topic )
        p.published_count += 1
        p.text      = text
        p.start_time = time.monotonic()
        self.mqtt.publish( topic, text, qos=0 )

    def on_connect(self, client, flags, rc, properties):
        log.info("MQTT connected")
        self.is_connected = True
        for topic in self._subscriptions:
            self.mqtt.subscribe( topic )

    def on_disconnect(self, client, packet, exc=None):
        log.info("MQTT disconnected")
        self.is_connected = False

    def on_subscribe(self, client, mid, qos, properties):
        pass

    def subscribe_callback( self, topic, callback ):
        l = self._subscriptions.setdefault( topic, [] ) # insert into callback directory
        if self.is_connected and not l:
            self.mqtt.subscribe( topic )                # if it's not in there already, we have to subscribe
        if callback not in l:
            l.append( callback )
        print( "MQTT: registered callback for %s on %s" % (topic, callback.__name__) )

    async def on_message(self, client, topic, payload, qos, properties):
        async def try_topic( t ):
            if cb_list := self._subscriptions.get( t ):
                for cb in cb_list:
                    await cb( topic, payload, qos, properties )

        await try_topic( topic )
        cur = Path( topic )
        while cur:
            parent = cur.dirname()
            await try_topic( parent / "#" )
            cur = parent
# """
    #   Decorates a method as a MQTT callback
    #
    @classmethod
    def decorate_callback( cls, topic, datatype=str, validation=None ):

        if hasattr( validation, "__contains__" ):
            def validator( payload ):
                if payload in validation:
                    return True
                log.error( "MQTT callback: Out of range: topic %s expects %s, received %r" % (topic, validation, payload))

        elif validation:
            def validator( payload ):
                if validation( payload ):
                    return True
                log.error( "MQTT callback: Invalid value: topic %s received %r" % (topic, payload))

        else:
            def validator( payload ):
                return True

        def decorator( func ):
            @functools.wraps( func )
            async def callback( self, topic, payload, qos, properties ):
                try:
                    payload = datatype( payload )
                except Exception as e:
                    log.error( "MQTT callback: %s: topic %s expects %s, received %r" % (e, topic, datatype, payload))
                    return
                if not validator( payload ):
                    return
                # log.info("MQTT callback: %s(%s,%s,%s,%s,%s)", func, self, topic, payload, qos, properties)
                await func( self, topic, payload, qos, properties )
            callback.mqtt_topic = topic
            cls._callbacks_generated.add( callback )
            return callback
        return decorator

    #   Subscribes and registers callbacks defined with the previous function
    def register_callbacks( self, obj, mqtt_topic="cmnd/" ):
        for attr in dir( obj ):
            method = getattr( obj, attr )
            if callable(method) and hasattr(method,"mqtt_topic"):
                if func := getattr( method, "__func__", None):
                    if func in self._callbacks_generated:
                        self.subscribe_callback( mqtt_topic + obj.mqtt_topic + method.mqtt_topic, method )

"""
    A value that can be updated by MQTT.
"""
class MQTTVariable:
    def __init__( self, mqtt_topic, container, name, datatype, validation, value, callback = None, mqtt_prefix="" ):
        assert not hasattr( container, name )
        setattr( container, name, self )
        self.container      = container
        self.mqtt_topic     = mqtt_topic
        self.datatype       = datatype
        self.validation     = validation
        self.value          = self.prev_value = value
        self.data_timestamp = time.monotonic()
        self.mqtt           = container.mqtt
        if callback:
            self.updated_callback = callback
        self.set_value( value )
        self.mqtt.subscribe_callback( mqtt_prefix+self.mqtt_topic, self.async_callback )

    async def async_callback( self, topic, payload, qos=None, properties=None ):
        self.callback( payload, qos, properties )
        await self.updated_callback( self )

    async def updated_callback( self, dummy_arg ):
        pass

    def callback( self, payload, qos=None, properties=None ):
        try:
            try:    # convert datatype
                payload = self.datatype( payload )
            except Exception as e:
                raise ValueError( "MQTT callback: %s: topic %s expects %s, received %r" % (e, self.mqtt_topic, self.datatype, payload) )
            # validate value
            if hasattr( self.validation, "__contains__" ):
                if payload not in self.validation:
                    raise ValueError( "MQTT callback: Out of range: topic %s expects %s, received %r" % (self.mqtt_topic, self.validation, payload) )
            elif self.validation:
                if not self.validation( payload ):
                    raise ValueError( "MQTT callback: Invalid value: topic %s received %r" % (self.mqtt_topic, payload) )
        except Exception as e:
            log.exception( "MQTTVariable" )
            raise

        self.prev_value = self.value
        self.value = payload
        self.data_timestamp = time.monotonic()
        self.publish()

    def set_value( self, value ):
        return self.callback( value )

    def set( self, value ):
        return self.callback( value )

    def publish( self ):
        return

"""
    A value (usually a configuration setting) that can be updated by MQTT.

    This will subscribe to a topic, listen, and when it receives a message
        - validate the value
        - update the value
        - publish it so everyone knows

    In addition it creates a /setting callback in the parent to list all settings.
"""
class MQTTSetting( MQTTVariable ):
    def __init__( self, container, name, datatype, validation, value, callback = None, mqtt_prefix="cmnd/" ):
        super().__init__( container.mqtt_topic+name, container, name, datatype, validation, value, callback, mqtt_prefix )
        self.mqtt.subscribe_callback( mqtt_prefix+self.container.mqtt_topic+"settings", self.async_publish_callback )  # ask to publish all settings

    async def async_publish_callback( self, topic, payload, qos=None, properties=None ):
        self.publish()

    def publish( self ):
        log.info( "MQTTSetting: %s = %s", self.mqtt_topic, self.value )
        self.mqtt.publish_value( self.mqtt_topic, self.value )

