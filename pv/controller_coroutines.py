#!/usr/bin/python
# -*- coding: utf-8 -*-

import os, time, sys, logging, orjson

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
pv.reload.add_module_to_reload( "pv.controller_coroutines" )
def on_module_unload():
    pass


########################################################################################
#
#   Fill Fakemeter fields
#
########################################################################################
async def power_coroutine( module_updated, first_start, self ):
    if first_start:
        await asyncio.sleep(2)      # wait for Solis to read all its registers (or fail if it is offline)

    last_power = None
    step_counter = BoundedCounter( 0, -10, 10 )
    while True:
        try:
            if module_updated(): # Exit if this module was reloaded
                return

            await self.event_meter_update.wait()

            # Compute power metrics
            #
            # Power consumed by the house loads not including inverters. Used for display and statistics, not used for routing.
            # This is (Main spartmeter) - (inverter spartmeters). It is accurate and fast.
            meter_power              = self.meter_total_power.value or 0  
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
                elif solis.fake_meter_lag.data_timestamp > time.monotonic()-5:
                    # if COM port is disconnected, use the smartmeter connection to check if the inverter is on
                    inverters_online.append( solis )

                lm = solis.local_meter
                lm_power = 0
                if lm.is_online:    # inverter local smartmeter power (negative for export)
                    lm_power = solis.local_meter.active_power.value or 0
                    total_grid_port_power += lm_power
                    meters_online += 1

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

            #   Fake Meter
            # shift slightly to avoid import when we can afford it
            if total_battery_power > 200:
                meter_power_tweaked += self.bms_soc.value*total_battery_power*0.0001
            total_input_power  = total_pv_power + total_grid_port_power

            fmdata = { "data_timestamp": self.meter_total_power.data_timestamp }

            fp = meter_power_tweaked

            #   Insert full impulse response into fakemeter
            #
            #

            # if last_power != None:
            #     delta = fp - last_power
            #     if abs(delta) > 50:
            #         fp += delta
            #         print( "%5d %5d added" % (fp, delta))
            #     else:
            #         print( "%5d %5d" % (fp, delta))

            # last_power = meter_power_tweaked

            # if abs(fp) < 100:
            #     step_counter.add( -1 )
            # else:
            #     # if step_counter.add( 1 ) < 0:
            #     fp *= 2

            # print( "%5d %5d" % (meter_power_tweaked, fp))

            for solis in self.inverters:
                if len( inverters_online ) == 1:
                    fake_power = fp
                else:
                    # balance power between inverters
                    # if len( inverters_with_battery ) == 2:
                    #     fake_power = ( meter_power_tweaked * 0.5 
                    #                     + 0.05*(solis.battery_power.value - total_battery_power*0.5)
                    #                     - 0.01*(solis.pv_power.value - total_pv_power*0.5) )
                    # else:
                    fake_power = fp * 0.5 + 0.05*(solis.input_power.value - total_input_power*0.5)
                fmdata[solis.key] = { "active_power"   : fake_power, }
            # Send data to fakemeters
            self.mqtt.mqtt.publish( "nolog/pv/fakemeter_update", orjson.dumps( fmdata ), qos=0 )
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

        except Exception:
            log.exception("PowerManager coroutine:")

########################################################################################
#
#   FakeMeter callback before serving values to inverter
#
########################################################################################
def fakemeter_on_getvalues( self ):
    try:
        # print( self.key, self.active_power.value )
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
    while True:
        if module_updated(): # Exit if this module was reloaded
            return

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
    bat_power_avg = { _.key: MovingAverage(10) for _ in self.inverters }
    if first_start:
        await asyncio.sleep( 5 )
    while True:
        if module_updated(): # Exit if this module was reloaded
            return

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


