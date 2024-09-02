#!/usr/bin/python
# -*- coding: utf-8 -*-

import time, asyncio, datetime, logging, collections, traceback, pymodbus
from pymodbus.exceptions import ModbusException
from asyncio.exceptions import TimeoutError

# Device wrappers and misc local libraries
import grugbus
from grugbus.devices import Eastron_SDM120
import config
from misc import *

log = logging.getLogger(__name__)

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
            
    async def read_coroutine( self ):
        tick = Metronome( config.POLL_PERIOD_SOLIS_METER )
        timeout_counter = 0
        await self.connect()
        while True:
            for reg_set in self.reg_sets:
                await tick.wait()
                pub = {}
                try:
                    regs = await self.read_regs( reg_set )
                    pub = { reg.key: reg.format_value() for reg in regs }
                    timeout_counter = 0
                    self.mqtt.publish( self.mqtt_topic, pub )

                except (TimeoutError, ModbusException):
                    timeout_counter += 1
                    if timeout_counter > 10:    self.active_power.value = 0 # if it times out, it's probably gone offgrid
                    else:                       timeout_counter += 1
                except Exception:
                    log.exception(self.key+":")
                    # s = traceback.format_exc()
                    # log.error(self.key+":"+s)
                    # self.mqtt.mqtt.publish( "pv/exception", s )
                    await asyncio.sleep(5)

                # wake up other coroutines waiting for fresh values
                self.event_power.set()
                self.event_power.clear()

            # wake up other coroutines waiting for fresh values
            self.event_all.set()
            self.event_all.clear()


