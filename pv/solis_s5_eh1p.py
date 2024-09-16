#!/usr/bin/python
# -*- coding: utf-8 -*-

import time, asyncio, datetime, logging, collections, traceback, pymodbus, orjson
from pymodbus.exceptions import ModbusException
from asyncio.exceptions import TimeoutError

# Device wrappers and misc local libraries
import grugbus
from grugbus.devices import Solis_S5_EH1P_6K_2020_Extras, Eastron_SDM120, Eastron_SDM630, Acrel_1_Phase, EVSE_ABB_Terra, Acrel_ACR10RD16TE4
from pv.mqtt_wrapper import MQTTWrapper
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
        self.mqtt        = mqtt
        self.mqtt_topic  = mqtt_topic
        self.mqtt_written_regs = {}
        mqtt.register_callbacks( self, "cmnd/" + mqtt_topic )

        # These are computed using values polled from the inverter
         # inverter reacts slower when battery works (charge or discharge), see comments in Router
        # self.battery_dcdc_active = grugbus.registers.FakeRegister( "battery_dcdc_active", True, "int", 0 )

        # For power routing we need to know battery power, in order to steal some of it when we want to.
        # The inverter's report of battery_power is slow (2 seconds lag) and does not account for energy stored
        # into the DC bus capacitors. However, for quick routing, we don't need it. What we need is the amount of power
        # going into the inverter: input_power = pv_power + grid_port_power
        # This has no lag, as pv_power is reported in real time.
        # TODO: this also includes backup output power, so we should substract it
        self.input_power = grugbus.registers.FakeRegister( "input_power", 0, "int", 0 )
        # self.battery_dcdc_power = grugbus.registers.FakeRegister( "battery_dcdc_power", 0, "int", 0 )

        self.mppt1_power       = grugbus.registers.FakeRegister( "mppt1_power", 0, "int", 0 )
        self.mppt2_power       = grugbus.registers.FakeRegister( "mppt2_power", 0, "int", 0 )
        self.bms_battery_power = grugbus.registers.FakeRegister( "bms_battery_power", 0, "int", 0 )

        #   Other coroutines that need inverter register values can wait on these events to 
        #   grab the values when they are read
        self.event_power = asyncio.Event()  # Fires every time frequent_regs below are read
        self.event_all   = asyncio.Event()  # Fires when all registers are read, for slower processes
        self.tick = Metronome( config.POLL_PERIOD_SOLIS )

        frequent_regs = [
                self.pv_power                   ,

                self.battery_voltage            ,
                self.battery_current            ,
                self.battery_current_direction  ,

                # self.battery_dcdc_direction,
                # self.battery_dcdc_current,
            ]

        self.reg_sets = [ frequent_regs + regs for regs in [[
            ],[
                self.energy_generated_today               ,  
                self.energy_generated_yesterday           ,      
            ],[
                self.mppt1_voltage                        ,
                self.mppt1_current                        ,
                self.mppt2_voltage                        ,
                self.mppt2_current                        ,
            ],[
                self.dc_bus_voltage                       ,
                self.dc_bus_half_voltage                  ,
                self.phase_a_voltage                      ,
            ],[
                self.temperature                          ,
                self.inverter_status                      ,
            ],[
                self.fault_status_1_grid                  ,
                self.fault_status_2_backup                ,
                self.fault_status_3_battery               ,
                self.fault_status_4_inverter              ,
                self.fault_status_5_inverter              ,
                self.operating_status                     ,
            ],[
                self.backup_voltage                       ,
            ],[
                self.bms_battery_soc                      ,
                self.bms_battery_health_soh               ,
                self.bms_battery_voltage                  ,
                self.bms_battery_current                  ,
                self.bms_battery_charge_current_limit     ,
                self.bms_battery_discharge_current_limit  ,
                self.bms_battery_fault_information_01     ,
                self.bms_battery_fault_information_02     ,
                self.backup_load_power                    ,
            ],[
                self.battery_charge_energy_today          ,
                self.battery_discharge_energy_today       ,
            ],[
                self.battery_max_charge_current           ,
                self.battery_max_discharge_current        ,
            ],[
                self.rwr_power_on_off                     ,              

                # TODO
                self.llc_bus_voltage,
                # self.switching_machine_setting,
                # self.b_limit_operation,
                # self.b_battery_status,

            ],[
                self.rwr_energy_storage_mode              ,
                self.rwr_backup_output_enabled            ,
            ]]]

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
                                (self.rwr_battery_charge_current_maximum_setting, 1000),
                                (self.rwr_battery_discharge_current_maximum_setting, 1000 )]:
                await reg.read()
                await reg.write_if_changed( value )

        except (TimeoutError, ModbusException): 
            # if inverter is disconnected because the Solis Wifi Stick is in, do not abort the rest of the program
            pass

        mqtt = self.mqtt
        topic = self.mqtt_topic
        while True:
            for reg_set in self.reg_sets:
                try:
                    await self.tick.wait()
                    try:
                        regs = await self.read_regs( reg_set, max_hole_size=4 )

                        #
                        #   Process values. Do not await until it is done, to prevent other tasks from seeing partial results
                        #   Code below is all conditional, depending on which registers were read
                        #

                        # Add polarity to battery parameters
                        # slow measured battery current
                        if self.battery_current_direction in regs:
                            regs.remove( self.battery_current_direction )
                            if self.battery_current_direction.value:    # positive current/power means charging, negative means discharging
                                self.battery_current.value     *= -1
                            self.battery_power.value       = self.battery_current.value * self.battery_voltage.value
                            regs.append( self.battery_power )

                        # # fast current setting using the DC/DC setpoint
                        # if self.battery_dcdc_direction in regs:
                        #     regs.remove( self.battery_dcdc_direction )
                        #     if self.battery_dcdc_direction.value:    # positive current/power means charging, negative means discharging
                        #         self.battery_dcdc_current.value *= -1
                        #     if (self.llc_bus_voltage.value or 0) < 50:        # fix: ignore current when DC/DC is off
                        #         self.battery_dcdc_current.value = 0
                        #     self.battery_dcdc_power.value = self.battery_dcdc_current.value * self.battery_voltage.value
                        #     regs.append( self.battery_dcdc_power )
                        
                        # Add useful metrics to avoid asof joins in database
                        if self.mppt1_voltage in regs:
                            self.mppt1_power.value = int( self.mppt1_current.value * self.mppt1_voltage.value )
                            self.mppt2_power.value = int( self.mppt2_current.value * self.mppt2_voltage.value )
                            regs.append( self.mppt1_power )
                            regs.append( self.mppt2_power )

                        if self.bms_battery_current in regs:
                            if self.battery_current_direction.value:    # positive current/power means charging, negative means discharging
                                self.bms_battery_current.value *= -1
                            self.bms_battery_power.value = int( self.bms_battery_current.value * self.bms_battery_voltage.value )
                            regs.append( self.bms_battery_power )

                        # Prepare MQTT publish
                        for reg in regs:
                            mqtt.publish_reg( topic, reg )

                    finally:
                        # wake up other coroutines waiting for fresh values
                        self.event_power.set()
                        self.event_power.clear()

                # wake up other coroutines waiting for fresh values

                except (TimeoutError, ModbusException):
                #     # use defaults so the rest of the code still works if connection to the inverter is lost
                #     # note self.is_online is set to False by grugbus.Device when communication fails, no need
                #     # to set it again here
                #     self.battery_current.value            = 0
                #     self.bms_battery_current.value        = 0
                #     self.battery_power.value              = 0
                #     self.battery_max_charge_current.value = 0
                #     self.battery_dcdc_active.value        = 1
                #     self.pv_power.value                   = 0
                #     self.input_power.value                = 0
                    await asyncio.sleep(1)

                except Exception:
                    self.is_online = False
                    log.exception(self.key+":")
                    # s = traceback.format_exc()
                    # log.error(self.key+":"+s)
                    # self.mqtt.mqtt.publish( "pv/exception", s )
                    await asyncio.sleep(1)

            self.event_all.set()
            self.event_all.clear()

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
                print( "Reg:", addr, "read", resp.registers[0] )

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








