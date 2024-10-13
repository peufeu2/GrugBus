#!/usr/bin/python
# -*- coding: utf-8 -*-

import time, asyncio, datetime, logging, collections, traceback, pymodbus, orjson
from pymodbus.exceptions import ModbusException
from asyncio.exceptions import TimeoutError

# Device wrappers and misc local libraries
import grugbus
from grugbus.devices import Solis_S5_EH1P_6K_2020_Extras, Eastron_SDM120, Eastron_SDM630, Acrel_1_Phase, EVSE_ABB_Terra, Acrel_ACR10RD16TE4
from pv.mqtt_wrapper import MQTTWrapper, MQTTVariable
from misc import *
import config

log = logging.getLogger(__name__)

########################################################################################
#
#       Solis inverter
#
#       This class communicates with the inverter's COM port
#
########################################################################################
class Solis( grugbus.SlaveDevice ):
    def __init__( self, modbus, modbus_addr, key, name, local_meter, fake_meter, mqtt, mqtt_topic ):
        super().__init__( modbus, modbus_addr, key, name, Solis_S5_EH1P_6K_2020_Extras.MakeRegisters() )

        self.local_meter = local_meter        # on AC grid port
        self.fake_meter  = fake_meter    # meter emulation on meter port
        fake_meter.solis = self
        self.mqtt        = mqtt
        self.mqtt_topic  = mqtt_topic
        self.mqtt_written_regs = {}
        mqtt.register_callbacks( self )

        # Get fake meter lag from Controller
        # MQTTVariable( self.mqtt_topic+"fakemeter/lag", self, "fake_meter_lag" , float, None, 0 )

        # For power routing we need to know battery power, in order to steal some of it when we want to.
        # The inverter's report of battery_power is slow and does not account for energy stored
        # into the DC bus capacitors. However, for quick routing, we don't need it. What we need is the amount of power
        # going into the inverter: input_power = pv_power + grid_port_power
        # This has no lag, as pv_power is reported in real time.
        # TODO: this also includes backup output power, so we should substract it
        # TODO: new battery current register reacts much faster
        self.input_power = grugbus.registers.FakeRegister( "input_power", 0, "int", 0 )

        self.mppt1_power       = grugbus.registers.FakeRegister( "mppt1_power", 0, "int", 0 )
        self.mppt2_power       = grugbus.registers.FakeRegister( "mppt2_power", 0, "int", 0 )

        #   Other coroutines that need inverter register values can wait on these events to 
        #   grab the values when they are read
        self.event_power = asyncio.Event()  # Fires every time frequent_regs below are read
        self.event_all   = asyncio.Event()  # Fires when all registers are read, for slower processes
        self.tick = Metronome( config.POLL_PERIOD_SOLIS )

        #   TODO: add +1.5A offset to solis2 new battery current register, 0A offset on solis1
        #   TODO: new battery current register returns 0 when inverter is off, check if it also does when battery is full
        #   TODO: new battery current register behavior when fully charged

        frequent_regs = [
                #33049 - 33057
                self.mppt1_voltage              ,
                self.mppt1_current              ,
                self.mppt2_voltage              ,
                self.mppt2_current              ,
                self.pv_power                   ,

                self.battery_current            ,

                self.battery_current_direction  ,
            ]

        all_regs = [
                self.energy_generated_today               ,  
                self.energy_generated_yesterday           ,      

                self.battery_charge_energy_today          ,

                self.phase_a_voltage                      ,

                self.temperature                          ,
                self.inverter_status                      ,

                self.fault_status_1_grid                  ,
                self.fault_status_2_backup                ,
                self.fault_status_3_battery               ,
                self.fault_status_4_inverter              ,
                self.fault_status_5_inverter              ,
                self.operating_status                     ,

                self.backup_voltage                       ,
                self.battery_voltage                      ,
                # self.bms_battery_soc                      ,
                # self.bms_battery_health_soh               ,
                # self.bms_battery_voltage                  ,
                # self.bms_battery_current                  ,
                # self.bms_battery_charge_current_limit     ,
                # self.bms_battery_discharge_current_limit  ,
                # self.bms_battery_fault_information_01     ,
                # self.bms_battery_fault_information_02     ,
                self.backup_load_power                    ,

                self.battery_charge_energy_today          ,
                self.battery_discharge_energy_today       ,

                self.battery_max_charge_current           ,
                self.battery_max_discharge_current        ,

                self.rwr_power_on_off                     ,              

                self.rwr_energy_storage_mode              ,
                self.rwr_backup_output_enabled            ,
            ]

        # Build modbus requests: read frequent_regs on every request, plus one chunk out of all_regs
        self.reg_sets = list( self.reg_list_interleave( frequent_regs, all_regs ) )

    async def read_coroutine( self ):
        # set meter type remotely to make it easy to emulate different fakemeters
        if   self.fake_meter.meter_type == Acrel_1_Phase:      mt = 1
        elif self.fake_meter.meter_type == Acrel_ACR10RD16TE4: mt = 2
        elif self.fake_meter.meter_type == Eastron_SDM120:     mt = 4
        mt |= {"grid":0x100, "load":0x200}[self.fake_meter.meter_placement]

        try:
            await self.adjust_time()
            await self.rwr_meter1_type_and_location.read()
            await self.rwr_meter1_type_and_location.write_if_changed( mt )

            # configure inverter
            for reg, value in  [(self.rwr_meter1_type_and_location, mt), 
                                (self.rwr_battery_charge_current_maximum_setting, 100.0),
                                (self.rwr_battery_discharge_current_maximum_setting, 100.0)]:
                await reg.read()
                await reg.write_if_changed( value )

        except (TimeoutError, ModbusException): 
            # if inverter is disconnected because the Solis Wifi Stick is in, do not abort the rest of the program
            pass

        mqtt = self.mqtt
        topic = self.mqtt_topic
        startup_done = False
        while True:
            # At startup, read all registers at once, do not wait.
            for n, reg_set in enumerate( self.reg_sets if startup_done else [ list(set(sum(self.reg_sets,[]))) ] ):
                try:
                    await self.tick.wait()
                    try:
                        regs = set( await self.read_regs( reg_set, max_hole_size=8 ) )

                        #
                        #   Process values. Do not await until it is done, to prevent other tasks from seeing partial results
                        #   Code below is all conditional, depending on which registers were read
                        #

                        # Add polarity to battery parameters
                        if self.battery_current_direction in regs:
                            regs.remove( self.battery_current_direction )
                            if self.battery_current_direction.value:    # positive current/power means charging, negative means discharging
                                self.battery_current.value     *= -1

                            # offset calibration
                            if f := config.CALIBRATION.get( self.mqtt_topic + "battery_current"):
                                self.battery_current.value = f( self.battery_current.value )

                            self.battery_power.value       = self.battery_current.value * self.battery_voltage.value
                            regs.add( self.battery_power )

                        # Add useful metrics to avoid asof joins in database
                        if self.mppt1_voltage in regs:
                            self.mppt1_power.value = int( self.mppt1_current.value * self.mppt1_voltage.value )
                            self.mppt2_power.value = int( self.mppt2_current.value * self.mppt2_voltage.value )
                            regs.add( self.mppt1_power )
                            regs.add( self.mppt2_power )

                        # if self.bms_battery_current in regs:
                        #     if self.battery_current_direction.value:    # positive current/power means charging, negative means discharging
                        #         self.bms_battery_current.value *= -1
                            # self.bms_battery_power.value = int( self.bms_battery_current.value * self.bms_battery_voltage.value )
                            # regs.add( self.bms_battery_power )

                        # Prepare MQTT publish
                        for reg in regs:
                            mqtt.publish_reg( topic, reg )

                        if config.LOG_MODBUS_REQUEST_TIME_SOLIS:
                            self.publish_modbus_timings()

                        self.mqtt.mqtt.publish( "nolog/event/" + self.key, qos=0 )

                    finally:
                        # wake up other coroutines waiting for fresh values
                        self.event_power.set()
                        self.event_power.clear()


                except (TimeoutError, ModbusException):
                    # note grugbus.Device logs the exception and sets self.is_online is set to False
                    # when communication fails, no need to do it again here
                    await asyncio.sleep(1)

                except Exception:
                    self.is_online = False
                    log.exception(self.key+":")
                    await asyncio.sleep(1)

            # wake up other coroutines waiting for fresh values
            self.event_all.set()
            self.event_all.clear()
            startup_done = True

            # reload config if changed
            self.tick.set( config.POLL_PERIOD_SOLIS )

    def is_ongrid( self ):
        return not self.is_offgrid()

    def is_offgrid( self ):
        return self.fault_status_1_grid.bit_is_active( "No grid" )

    def get_time_regs( self ):
        return ( self.rwr_real_time_clock_year, self.rwr_real_time_clock_month,  self.rwr_real_time_clock_day,
         self.rwr_real_time_clock_hour, self.rwr_real_time_clock_minute, self.rwr_real_time_clock_seconds )

    async def get_time( self ):
        regs = self.get_time_regs()
        await self.read_regs( regs )
        dt = [ reg.value for reg in regs ]
        dt[0] += 2000
        return datetime.datetime( *dt )

    async def set_time( self, dt  ):
        self.rwr_real_time_clock_year   .value = dt.year - 2000   
        self.rwr_real_time_clock_month  .value = dt.month   
        self.rwr_real_time_clock_day    .value = dt.day   
        self.rwr_real_time_clock_hour   .value = dt.hour   
        self.rwr_real_time_clock_minute .value = dt.minute   
        self.rwr_real_time_clock_seconds.value = dt.second
        await self.write_regs( self.get_time_regs() )

    async def adjust_time( self ):
        inverter_time = await self.get_time()
        dt = datetime.datetime.now()
        log.info( "Inverter time: %s, Pi time: %s" % (inverter_time.isoformat(), dt.isoformat()))
        deltat = abs( dt-inverter_time )
        if deltat < datetime.timedelta( seconds=2 ):
            log.info( "Inverter time is OK, we won't set it." )
        else:
            if deltat > datetime.timedelta( seconds=4000 ):
                log.info( "Pi time seems old, is NTP active?")
            else:
                log.info( "Setting inverter time to Pi time" )
                await self.set_time( dt )
                inverter_time = await self.get_time()
                log.info( "Inverter time: %s, Pi time: %s" % (inverter_time.isoformat(), dt.isoformat()))

    @MQTTWrapper.decorate_callback( "read_regs", orjson.loads )
    async def cb_read_regs( self, topic, payload, qos, properties ):
        print("Callback:", self.key, topic, payload )
        for addr in payload:
            if reg := self.regs_by_key.get( addr ) or self.regs_by_addr.get( addr ):
                await reg.read()
                print( "Reg:", reg.key, "read", reg.value )
            else:
                resp = await self.modbus.read_holding_registers( addr, 1, self.bus_address )
                print( resp )
                print( "Reg:", addr, "read", resp.registers )

    @MQTTWrapper.decorate_callback( "write_regs", orjson.loads )
    async def cb_write_regs( self, topic, payload, qos, properties ):
        print("Callback:", self.key, topic, payload )
        for addr, value in payload:
            if reg := self.regs_by_key.get( addr ) or self.regs_by_addr.get( addr ):
                old_value = await reg.read()
                if reg.key not in self.mqtt_written_regs:
                    self.mqtt_written_regs[reg.key] = old_value
                await reg.write( value )
                print( "Reg:", reg.key, "read", old_value, "write", value, "read", await reg.read() )
            else:
                resp = await self.modbus.read_holding_registers( addr, 1, self.bus_address )
                old_value = resp.registers[0]
                if addr not in self.mqtt_written_regs:
                    self.mqtt_written_regs[addr] = old_value
                await self.modbus.write_register( addr, value, self.bus_address )
                resp = await self.modbus.read_holding_registers( addr, 1, self.bus_address )
                print( "Reg:", addr, "read", old_value, "write", value, "read", resp.registers[0] )








