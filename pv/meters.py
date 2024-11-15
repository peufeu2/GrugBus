#!/usr/bin/python
# -*- coding: utf-8 -*-

import time, asyncio, logging, collections
from pymodbus.exceptions import ModbusException
from asyncio.exceptions import TimeoutError

# Device wrappers and misc local libraries
import grugbus
from grugbus.devices import Eastron_SDM630, Eastron_SDM120
import config
from misc import *

log = logging.getLogger(__name__)


########################################################################################
#
#       Main house smartmeter, grid side, meters total power for solar+home
#
#       This class reads the meter and publishes it on MQTT
#
#       Meter is read very often, so it gets its own serial port
#
########################################################################################
class SDM630( grugbus.SlaveDevice ):
    def __init__( self, modbus, modbus_addr, key, name, mqtt, mqtt_topic ):
        super().__init__( modbus, modbus_addr, key, name, Eastron_SDM630.MakeRegisters() ),
        self.mqtt        = mqtt
        self.mqtt_topic  = mqtt_topic

        self.total_power_tweaked = 0.0
        self.event_power = asyncio.Event()  # Fires every time frequent_regs below are read
        self.event_all   = asyncio.Event()  # Fires when all registers are read, for slower processes
        self.tick = Metronome(config.POLL_PERIOD_METER)  # fires a tick on every period to read periodically, see misc.py

        # For power routing to work we need to read total_power frequently. So we don't read 
        # ALL registers every time. Instead, gather the unimportant ones in little groups
        # and frequently read THE important register (total_power) + one group.
        # Unimportant registers will be updated less often, who cares.
        frequent_regs = [ 
            self.total_power    ,
            self.phase_1_power  ,
            self.phase_2_power  ,
            self.phase_3_power  ,
        ]
        all_regs = [
            self.total_volt_amps                  ,    # required for fakemeter
            self.total_var                        ,    # required for fakemeter
            self.total_power_factor               ,    # required for fakemeter
            self.total_phase_angle                ,    # required for fakemeter
            self.frequency                        ,    # required for fakemeter
            self.total_import_kwh                 ,    # required for fakemeter
            self.total_export_kwh                 ,    # required for fakemeter
            self.total_import_kvarh               ,    # required for fakemeter
            self.total_export_kvarh               ,    # required for fakemeter
            self.phase_1_line_to_neutral_volts    ,    # required for fakemeter
            self.phase_2_line_to_neutral_volts    ,
            self.phase_3_line_to_neutral_volts    ,
            self.phase_1_current                  ,    # required for fakemeter
            self.phase_2_current                  ,    # but current has no sign
            self.phase_3_current                  ,
            self.phase_1_volt_amps                ,    # 
            self.phase_2_volt_amps                ,
            self.phase_3_volt_amps                ,
            self.total_kwh                        ,    # required for fakemeter
            self.total_kvarh                      ,    # required for fakemeter
            self.average_line_to_neutral_volts_thd,
            self.average_line_current_thd         ,
            self.total_import_kwh,
            self.total_export_kwh,
        ]

        self.reg_sets = list( self.reg_list_interleave( frequent_regs, all_regs ) )

    async def read_coroutine( self ):
        mqtt  = self.mqtt
        topic = self.mqtt_topic
        while True:
            for reg_set in self.reg_sets:
                try:
                    await self.tick.wait()
                    try:
                        regs = await self.read_regs( reg_set )
                    finally:
                        # wake up other coroutines waiting for fresh values
                        # even if there was a timeout
                        self.event_power.set()
                        self.event_power.clear()

                    for reg in regs:
                         mqtt.publish_reg( topic, reg )

                    mqtt.publish_value( topic+"is_online", int( self.is_online ))   # set by read_regs(), True if it succeeded, False otherwise

                    if config.MAINBOARD_FLASH_LEDS:
                        self.mqtt.mqtt.publish( "nolog/pv/event/" + self.key, qos=0 )

                except (TimeoutError, ModbusException):
                    await asyncio.sleep(1)

                except Exception:
                    self.is_online = False
                    log.exception(self.key+":")
                    await asyncio.sleep(0.5)

            # wake up other coroutines waiting for fresh values
            self.event_all.set()
            self.event_all.clear()

            # reload config if changed
            self.tick.set(config.POLL_PERIOD_METER)


########################################################################################
#
#       Local smartmeter on inverter grid port (not the global one for the house)
#
########################################################################################
class SDM120( grugbus.SlaveDevice ):
    def __init__( self, modbus, modbus_addr, key, name, mqtt, mqtt_topic ):
        super().__init__( modbus, modbus_addr, key, name, Eastron_SDM120.MakeRegisters() ),
        self.mqtt        = mqtt
        self.mqtt_topic  = mqtt_topic

        #   Other coroutines that need register values can wait on these events to 
        #   grab the values when they are read
        self.event_power = asyncio.Event()  # Fires every time frequent_regs below are read
        self.event_all   = asyncio.Event()  # Fires when all registers are read, for slower processes
        self.tick = Metronome( config.POLL_PERIOD_SOLIS_METER )
        self.reg_sets = [[self.active_power]] * 9 + [[
            self.active_power          ,
            # self.apparent_power        ,
            # self.reactive_power        ,
            # self.power_factor          ,
            # self.phase_angle           ,
            # self.frequency             ,
            self.import_active_energy  ,
            self.export_active_energy  ,
            # self.import_reactive_energy,
            # self.export_reactive_energy,
            # self.total_active_energy   ,
            # self.total_reactive_energy ,
        ]]
        self.power_history = collections.deque( maxlen=3 )

    async def read_coroutine( self ):
        mqtt  = self.mqtt
        topic = self.mqtt_topic

        while True:
            for reg_set in self.reg_sets:
                try:
                    await self.tick.wait()
                    try:
                        regs = await self.read_regs( reg_set )
                        self.power_history.append( self.active_power.value )
                    finally:
                        # wake up other coroutines waiting for fresh values
                        self.event_power.set()
                        self.event_power.clear()

                    for reg in regs:
                        mqtt.publish_reg( topic, reg )

                    if config.MAINBOARD_FLASH_LEDS:
                        self.mqtt.mqtt.publish( "nolog/pv/event/" + self.key, qos=0 )

                except (TimeoutError, ModbusException):
                    await asyncio.sleep(1)

                except Exception:
                    self.is_online = False
                    log.exception(self.key+":")
                    await asyncio.sleep(1)

            # wake up other coroutines waiting for fresh values
            self.event_all.set()
            self.event_all.clear()

            # reload config if changed
            self.tick.set(config.POLL_PERIOD_METER)


