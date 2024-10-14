#!/usr/bin/python
# -*- coding: utf-8 -*-

import time, asyncio, logging, collections, sys
from pymodbus.exceptions import ModbusException
from asyncio.exceptions import TimeoutError

# Device wrappers and misc local libraries
import grugbus
from grugbus.devices import EVSE_ABB_Terra
from pv.mqtt_wrapper import MQTTWrapper, MQTTSetting
import config
from misc import *

log = logging.getLogger(__name__)


########################################################################################
#
#       ABB Terra EVSE, controlled via Modbus
#
#       See grugbus/devices/EVSE_ABB_Terra.txt for important notes!
#
########################################################################################
class EVSE( grugbus.SlaveDevice ):
    def __init__( self, modbus, modbus_addr, key, name, local_meter, mqtt, mqtt_topic ):
        super().__init__( modbus, modbus_addr, key, name, EVSE_ABB_Terra.MakeRegisters() ),
        self.mqtt        = mqtt
        self.mqtt_topic  = mqtt_topic
        self.local_meter = local_meter

        # Modbus polling
        self.tick      = Metronome( config.POLL_PERIOD_EVSE )   # how often we poll it over modbus
        self.rwr_current_limit.value = 0.0
        self.regs_to_read = (
            self.charge_state       ,
            self.current_limit      ,
            # self.current            ,
            # self.active_power       ,
            self.energy             ,
            self.error_code         ,
            self.socket_state
        )

        # Fires when all registers are read, if some other process wants to read them
        self.event_all = asyncio.Event() 
        self.resend_current_limit_tick = Metronome( 10 )    # sometimes the EVSE forgets the setting, got to send it once in a while

    async def read_coroutine( self ):
        mqtt = self.mqtt
        topic = self.mqtt_topic
        while True:
            try:
                await self.tick.wait()
                for reg in await self.read_regs( self.regs_to_read ):
                    mqtt.publish_reg( topic, reg )

                # publish force charge info                
                if config.LOG_MODBUS_REQUEST_TIME_ABB:
                    self.publish_modbus_timings()

                if config.MAINBOARD_FLASH_LEDS:
                    self.mqtt.mqtt.publish( "nolog/pv/event/" + self.key )

                # republish current limit periodically
                await self.set_current_limit( self.rwr_current_limit.value )

            except (TimeoutError, ModbusException):
                await asyncio.sleep(1)

            except Exception:
                log.exception(self.key+":")
                await asyncio.sleep(1)

            self.event_all.set()
            self.event_all.clear()

    async def set_current_limit( self, current_limit ):
        current_limit = round(current_limit)
        # print("set limit", current_limit, "minmax", self.current_limit_bounds.minimum, self.current_limit_bounds.maximum, "value", self.current_limit_bounds.value, "reg", self.rwr_current_limit.value )

        if self.resend_current_limit_tick.ticked():        
            # sometimes the EVSE forgets the current limit, so send it at regular intervals
            # even if it did not change
            await self.rwr_current_limit.write( current_limit )
            wrote = True
        else:
            # otherwise write only if changed
            wrote = await self.rwr_current_limit.write_if_changed( current_limit )

        if wrote:
            # print( "write limit", current_limit )
            mqtt = self.mqtt
            topic = self.mqtt_topic
            mqtt.publish_reg( topic, self.rwr_current_limit )
            if config.LOG_MODBUS_WRITE_REQUEST_TIME:
                self.publish_modbus_timings()


#
#   Controller code moved to pv/router.py
#