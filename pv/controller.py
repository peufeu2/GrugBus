#!/usr/bin/python
# -*- coding: utf-8 -*-

import os, time, sys, logging, orjson, shutil, collections

# This program is supposed to run on a potato (Allwinner H3 SoC) and uses async/await,
# so import the fast async library uvloop
import asyncio
#aiohttp, aiohttp.web

# Modbus
from pymodbus.exceptions import ModbusException
from asyncio.exceptions import TimeoutError, CancelledError

# Device wrappers and misc local libraries
import config
from misc import *
import pv.reload


log = logging.getLogger(__name__)

# This module can be reloaded at runtime if the file is modified.
def on_module_unload():
    pass

########################################################################################
#
#   - Compute and publish totals across inverters
#   - Fill Fakemeter fields for inverters
#
########################################################################################
async def power_coroutine( module_updated, first_start, self ):
    if first_start:
        await asyncio.sleep(3)      # wait for Solis to read all its registers (or fail if it is offline)

    m = self.meter
    await m.event_all.wait()    # wait for all registers to be read

    boost = 0

    chrono = Chrono()
    while not module_updated(): # Exit if this module was reloaded
        try:
            await m.event_power.wait()
            time_since_last = chrono.lap()

            # Compute power metrics
            #
            # Power consumed by the house loads not including inverters. Used for display and statistics, not used for routing.
            # This is (Main spartmeter) - (inverter spartmeters). It is accurate and fast.
            meter_power              = self.meter.total_power.value or 0  
            meter_power_tweaked      = meter_power
            total_pv_power           = 0
            total_input_power        = 0
            total_grid_port_power    = 0
            total_battery_power      = 0
            battery_max_charge_power = 0

            inverters_with_battery  = []
            inverters_online        = []
            meters_online           = 0

            # TODO: degraded modes
            for solis in self.inverters:
                #   For power sharing (in fakemeter below) we need to know if both inverters are ongrid and capable
                # of producing power, or just one. If one is marked as online when there are two, powersharing will cause oscillations.
                # if two are marked online when there is one, it will simply react slower, so it's not really a problem.
                # Note: solis.inverter_status.value == 3 means it is ongrid and producing, but
                # for some errors like CAN FAIL, it will still produce while this register is set to something else
                # like "turning off"... same for operating_status...
                if solis.is_online:
                    if not (solis.is_offgrid() or solis.rwr_power_on_off.value == solis.rwr_power_on_off.value_off):
                        inverters_online.append( solis )
                # if COM port is disconnected, use the smartmeter connection to check if the inverter is on
                elif solis.fake_meter.last_query_time > time.monotonic()-5:
                    inverters_online.append( solis )

                # inverters local smartmeters power (negative for export)
                lm = solis.local_meter
                lm_power = 0
                if lm.is_online:
                    lm_power = solis.local_meter.active_power.value or 0
                    total_grid_port_power += lm_power
                    meters_online += 1

                # PV and battery
                pv_power = 0
                solis.input_power.value = 0
                if solis.is_online:                        
                    battery_max_charge_power += (solis.battery_max_charge_current.value or 0) * (solis.battery_voltage.value or 0)
                    
                    # Battery charging power. Positive if charging, negative if discharging. Use fast register.
                    total_battery_power += solis.battery_power.value or 0                    
                    pv_power = solis.pv_power.value or 0
                    total_pv_power += pv_power

                    if lm.is_online:
                        solis.input_power.value = pv_power + lm_power
                    else:
                        # If meter offline, use inverter's measured battery power instead
                        solis.input_power.value = solis.battery_power.value

            #   Fake Meter
            # shift slightly to avoid import when we can afford it
            if total_battery_power > 200:
                meter_power_tweaked += self.bms_soc.value*total_battery_power*0.0001
            total_input_power  = total_pv_power + total_grid_port_power

            #   Insert full impulse response into fakemeter
            #
            #
            for solis in self.inverters:
                if len( inverters_online ) == 1:
                    fake_power = meter_power_tweaked
                else:
                    # balance power between inverters
                    # if len( inverters_with_battery ) == 2:
                    #     fake_power = ( meter_power_tweaked * 0.5 
                    #                     + 0.05*(solis.battery_power.value - total_battery_power*0.5)
                    #                     - 0.01*(solis.pv_power.value - total_pv_power*0.5) )
                    # else:
                    fake_power = meter_power_tweaked * 0.5 + 0.05*(solis.input_power.value - total_input_power*0.5)

                # This is good stuff
                # bonus = 0
                # for threshold, multiplier in (200,2), (500,1), (1000,1):
                #     if fake_power > threshold:
                #         bonus += (fake_power - threshold) * multiplier
                # if not bonus:
                #     boost = 1
                # else:
                #     fake_power = min( 15000, fake_power+bonus*boost )
                #     boost = max( 0, boost - time_since_last*0.3 )

                fm = solis.fake_meter
                fm.active_power           .value = fake_power
                fm.voltage                .value = m.phase_1_line_to_neutral_volts .value
                fm.current                .value = m.phase_1_current               .value
                fm.apparent_power         .value = m.total_volt_amps               .value
                fm.reactive_power         .value = m.total_var                     .value
                fm.power_factor           .value = (m.total_power_factor            .value % 1.0)
                fm.frequency              .value = m.frequency                     .value
                fm.import_active_energy   .value = m.total_import_kwh              .value
                fm.export_active_energy   .value = m.total_export_kwh              .value
                fm.data_timestamp                = m.last_transaction_timestamp
                fm.is_online                     = m.is_online

                fm.write_regs_to_context()

                self.mqtt.publish_reg( fm.mqtt_topic, fm.active_power )

            await asyncio.sleep(0)      # yield 

            # atomic update
            self.meter_power_tweaked      = meter_power_tweaked
            self.house_power              = meter_power - total_grid_port_power
            self.total_pv_power           = total_pv_power
            self.total_input_power        = total_input_power
            self.total_grid_port_power    = total_grid_port_power
            self.total_battery_power      = total_battery_power
            self.battery_max_charge_power = battery_max_charge_power
            self.event_power.set()
            self.event_power.clear()

            self.mqtt.publish_value( "pv/meter/house_power",         self.house_power              , int )
            self.mqtt.publish_value( "pv/total_pv_power",            self.total_pv_power           , int )
            self.mqtt.publish_value( "pv/total_battery_power",       self.total_battery_power      , int )
            self.mqtt.publish_value( "pv/total_input_power",         self.total_input_power        , int )
            self.mqtt.publish_value( "pv/total_grid_port_power",     self.total_grid_port_power    , int )
            self.mqtt.publish_value( "pv/battery_max_charge_power",  self.battery_max_charge_power , int )
            for solis in self.inverters:
                self.mqtt.publish_reg( solis.mqtt_topic, solis.input_power )

            # publish data for router
            d = { 
                "m_total_power"            : int( m.total_power.value ),
                "m_p1_v"                   : round( m.phase_1_line_to_neutral_volts.value, 1 ),
                "m_p2_v"                   : round( m.phase_2_line_to_neutral_volts.value, 1 ),
                "m_p3_v"                   : round( m.phase_3_line_to_neutral_volts.value, 1 ),
                "m_p1_i"                   : round( m.phase_1_current.value, 1),
                "m_p2_i"                   : round( m.phase_2_current.value, 1),
                "m_p3_i"                   : round( m.phase_3_current.value, 1),

                "meter_power_tweaked"      : int( self.meter_power_tweaked ),
                "house_power"              : int( self.house_power ),
                "total_pv_power"           : int( self.total_pv_power ),
                "total_input_power"        : int( self.total_input_power ),
                "total_grid_port_power"    : int( self.total_grid_port_power ),
                "total_battery_power"      : int( self.total_battery_power ),
                "battery_max_charge_power" : int( self.battery_max_charge_power ),

                "data_timestamp"           : m.last_transaction_timestamp
            }
            self.mqtt.mqtt.publish( "nolog/pv/router_data", orjson.dumps( d ) )

        except Exception:
            log.exception("PowerManager coroutine:")

# class RingBuffer:
#     def __init__( self, length ):
#         self.q = [0] * length
#         self.pos = 0

#     def cur( self ):
#         return self.q[self.pos]

#     def at( self, offset ):
#         return self.q[ (self.pos+offset) % len(self.q) ]

#     def set( self, offset, value ):
#         self.q[ (self.pos+offset) % len(self.q) ] = value

#     def pop( self ):
#         r = self.q[self.pos]
#         self.q[self.pos] = 0
#         self.pos = (self.pos+1) % len(self.q)
#         return r

#     def add( self, offset, value ):
#         self.q[ (self.pos+offset) % len(self.q) ] += value


########################################################################################
#
#   FakeMeter callback before serving values to inverter
#
########################################################################################
def fakemeter_on_getvalues( self ):
    try:
        return True
    except Exception:
        log.exception("fakemeter_on_getvalues")
        return True


    ########################################################################################
    #
    #   Blackout logic
    #
    ########################################################################################
    # async def inverter_blackout_coroutine( self, solis ):
    #     timeout_blackout  = Timeout( 120, expired=True )
    #     while True:
    #         await solis.event_all.wait()
    #         try:
    #             # Blackout logic: enable backup output in case of blackout
    #             blackout = solis.is_offgrid() and solis.phase_a_voltage.value < 100
    #             if blackout:
    #                 log.info( "Blackout" )
    #                 await solis.rwr_backup_output_enabled.write_if_changed( 1 )      # enable backup output
    #                 await solis.rwr_power_on_off         .write_if_changed( solis.rwr_power_on_off.value_on )   # Turn inverter on (if it was off)
    #                 await solis.rwr_energy_storage_mode  .write_if_changed( 0x32 )   # mode = Backup, optimal revenue, charge from grid
    #                 timeout_blackout.reset()
    #             elif not timeout_blackout.expired():        # stay in backup mode with backup output enabled for a while
    #                 log.info( "Remain in backup mode for %d s", timeout_blackout.remain() )
    #                 timeout_power_on.reset()
    #                 timeout_power_off.reset()
    #             else:
    #                 await solis.rwr_backup_output_enabled.write_if_changed( 0 )      # disable backup output
    #                 await solis.rwr_energy_storage_mode  .write_if_changed( 0x23 )   # Self use, optimal revenue, charge from grid

    #         except (TimeoutError, ModbusException): pass
    #         except Exception:
    #             log.exception("")
    #             await asyncio.sleep(5)           

########################################################################################
#
#   Low battery power save logic
#
########################################################################################
async def inverter_powersave_coroutine( module_updated, first_start, self, solis ):
    timeout_power_on  = Timeout( 60, expired=True )
    timeout_power_off = Timeout( 300 )
    power_reg = solis.rwr_power_on_off
    while not module_updated(): # Exit if this module was reloaded
        await solis.event_all.wait()
        if not solis.is_online:
            continue
        try:
            # Auto on/off: turn it off at night when the battery is below specified SOC
            # so it doesn't keep draining it while doing nothing useful
            cfg = config.SOLIS_POWERSAVE_CONFIG[ solis.key ]

            if not cfg["ENABLE_INVERTER"]:
                await power_reg.write_if_changed( power_reg.value_off )
            elif not cfg["ENABLE_POWERSAVE"]:
                await power_reg.write_if_changed( power_reg.value_on )
            else:
                mpptv = max( solis.mppt1_voltage.value, solis.mppt2_voltage.value )
                if power_reg.value == power_reg.value_on:
                    if mpptv < cfg["TURNOFF_MPPT_VOLTAGE"] and self.bms_soc.value <= cfg["TURNOFF_BATTERY_SOC"]:
                        timeout_power_on.reset()
                        if timeout_power_off.expired():
                            log.info("Powering OFF %s"%solis.key)
                            await power_reg.write_if_changed( power_reg.value_off )
                        else:
                            log.info( "Power off %s in %d s", solis.key, timeout_power_off.remain() )
                    else:
                        timeout_power_off.reset()
                else:
                    if mpptv > cfg["TURNON_MPPT_VOLTAGE"]:
                        timeout_power_off.reset()
                        if timeout_power_on.expired():
                            log.info("Powering ON %s"%solis.key)
                            await power_reg.write_if_changed( power_reg.value_on )
                        else:
                            log.info( "Power on %s in %d s", solis.key, timeout_power_on.remain() )
                    else:
                        timeout_power_on.reset()
                    
        except (TimeoutError, ModbusException): pass
        except Exception:
            log.exception("Powersave:")
            await asyncio.sleep(5)      

########################################################################################
#
#   start fan if temperature too high or average battery power high
#
########################################################################################
async def inverter_fan_coroutine( module_updated, first_start, self ):
    timeout_fan_off = Timeout( 60 )
    bat_power_avg = { _.key: MovingAverageSeconds(10) for _ in self.inverters }
    if first_start:
        await asyncio.sleep( 5 )
    while not module_updated(): # Exit if this module was reloaded
        await asyncio.sleep( 2 )
        try:
            temps = []
            avgp = []
            for solis in self.inverters:
                if solis.is_online:
                    avgp.append( bat_power_avg[solis.key].append( abs(solis.battery_power.value or 0) ) or 0 )
                    temps.append( solis.temperature.value )

            if temps and (max(temps)> 40 or max(avgp) > 2000):
                timeout_fan_off.reset()
                self.mqtt.publish_value( "cmnd/plugs/tasmota_t3/Power", 1 )
            elif min( temps ) < 35 and timeout_fan_off.expired():
                self.mqtt.publish_value( "cmnd/plugs/tasmota_t3/Power", 0 )

        except (TimeoutError, ModbusException): pass
        except:
            log.exception("")
            await asyncio.sleep(5)

########################################################################################
#   System info
########################################################################################
async def sysinfo_coroutine( module_updated, first_start, self ):
    prev_cpu_timings = None
    while not module_updated():
        await asyncio.sleep(10)
        with open("/proc/stat") as f:
            cpu_timings = [ int(_) for _ in f.readline().split()[1:] ]
            cpu_timings = cpu_timings[3], sum(cpu_timings)  # idle time, total time
            if prev_cpu_timings:
                self.mqtt.publish_value( "pv/cpu_load_percent", round( 100.0*( 1.0-(cpu_timings[0]-prev_cpu_timings[0])/(cpu_timings[1]-prev_cpu_timings[1]) ), 1 ))
            prev_cpu_timings = cpu_timings

        with open("/sys/devices/virtual/thermal/thermal_zone0/temp") as f:
            self.mqtt.publish_value( "pv/cpu_temp_c", round( int(f.read())*0.001, 1 ) )
        total, used, free = shutil.disk_usage("/")
        self.mqtt.publish_value( "pv/disk_space_gb", round( free/2**30, 2 ) )

