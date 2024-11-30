#!/usr/bin/python
# -*- coding: utf-8 -*-

import os, time, sys, logging, orjson, collections

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

class Ctx:
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
            total_battery_power      = 0
            total_grid_port_power    = 0
            battery_max_charge_power = 0
            total_energy_generated_today = 0
            total_battery_charge_energy_today = 0

            router_total_pv_power           = 0
            router_total_battery_power      = 0
            router_total_input_power        = 0
            router_battery_max_charge_power = 0

            inverters_with_battery  = []
            inverters_online        = []
            meters_online           = 0

            mppt_power = {}

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
                    total_energy_generated_today += solis.energy_generated_today.value
                    total_battery_charge_energy_today += solis.battery_charge_energy_today.value

                    max_cp = (solis.battery_max_charge_current.value or 0) * (solis.battery_voltage.value or 0)           
                    battery_max_charge_power += max_cp
                    
                    # Battery charging power. Positive if charging, negative if discharging. Use fast register.
                    total_battery_power += solis.battery_power.value or 0                    
                    pv_power = solis.pv_power.value or 0
                    total_pv_power += pv_power

                    if lm.is_online:
                        solis.input_power.value = pv_power + lm_power
                    else:
                        # If meter offline, use inverter's measured battery power instead
                        solis.input_power.value = solis.battery_power.value

                    total_input_power  += solis.input_power.value

                    # if inverter is offgrid, router can't use its power
                    if solis.is_ongrid():
                        mppt_power[solis.key] = { "1":solis.mppt1_power.value, "2":solis.mppt2_power.value }
                        router_battery_max_charge_power += max_cp
                        router_total_pv_power += pv_power
                        router_total_battery_power += solis.battery_power.value or 0                    
                        router_total_input_power  += solis.input_power.value

            #   Fake Meter
            # shift slightly to avoid import when we can afford it
            meter_power_tweaked = config.METER_POWER_TWEAKED( self, meter_power_tweaked, total_battery_power )

            try:
                # hack area for meter tweaks
                pass
            except Exception:
                log.exception("PowerManager coroutine:") 

            #   Insert full impulse response into fakemeter
            #
            #
            for solis in self.inverters:
                # default power management: if there are two inverters, balance battery charging between the two
                if len( inverters_online ) == 1:
                    # hack area for meter tweaks
                    fake_power = meter_power_tweaked
                else:
                    # balance power between inverters
                    # if len( inverters_with_battery ) == 2:
                    #     fake_power = ( meter_power_tweaked * 0.5 
                    #                     + 0.05*(solis.battery_power.value - total_battery_power*0.5)
                    #                     - 0.01*(solis.pv_power.value - total_pv_power*0.5) )
                    # else:
                    fake_power = meter_power_tweaked * 0.5 + config.INVERTER_BALANCE_FACTOR*(solis.input_power.value - total_input_power*0.5)

                try:

                    if config.FAKEMETER_IMPROVE_TRANSIENTS == 1:
                        bonus = 0
                        for threshold, multiplier in (200,2), (500,1), (1000,1):
                            if fake_power > threshold:
                                bonus += (fake_power - threshold) * multiplier

                        if not bonus and self.bms_soc.value < 97:
                            boost = min( 1, boost + time_since_last*0.3 )
                        else:
                            # if bonus and boost:
                                # print( "boost", fake_power, bonus*boost )
                            fake_power = min( 15000, fake_power+bonus*boost )
                            boost = max( 0, boost - time_since_last*0.3 )
                    elif config.FAKEMETER_IMPROVE_TRANSIENTS == 2:
                        pass
                        # positive and negative versions
                        # fp = abs( fake_power )
                        # for threshold, multiplier in (200,2), (500,1), (1000,1):
                        #     if fp > threshold:
                        #         bonus += (fp - threshold) * multiplier
                        # if not bonus:
                        #     boost = 1       # power is low: enable boost for next spike
                        # else:
                        #     # fake_power = min( 15000, fake_power+bonus*boost )
                        #     fp = min( 15000, fp+bonus*boost )
                        #     if fake_power < 0:
                        #         pass
                        #         # fake_power = -fp
                        #     else:
                        #         fake_power = fp

                except Exception:
                    log.exception("PowerManager coroutine:") 

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
                if m.is_online and not fm.is_online:
                    log.info("%s online", fm.name)
                    fm.startup_done = True
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

            self.mqtt.publish_value( "pv/meter/house_power",            self.house_power              , int )
            self.mqtt.publish_value( "pv/total_pv_power",               self.total_pv_power           , int )
            self.mqtt.publish_value( "pv/total_battery_power",          self.total_battery_power      , int )
            self.mqtt.publish_value( "pv/total_input_power",            self.total_input_power        , int )
            self.mqtt.publish_value( "pv/total_grid_port_power",        self.total_grid_port_power    , int )
            self.mqtt.publish_value( "pv/battery_max_charge_power",     self.battery_max_charge_power , int )
            self.mqtt.publish_value( "pv/energy_generated_today",       total_energy_generated_today )
            self.mqtt.publish_value( "pv/battery_charge_energy_today",  total_battery_charge_energy_today )

            for solis in self.inverters:
                self.mqtt.publish_reg( solis.mqtt_topic, solis.input_power )

            # publish data for router
            d = { 
                "meter_total_power"            : int( m.total_power.value ),
                "meter_phase_v"            : ( round( m.phase_1_line_to_neutral_volts.value, 1 ), round( m.phase_2_line_to_neutral_volts.value, 1 ), round( m.phase_3_line_to_neutral_volts.value, 1 ) ),
                "meter_phase_i"            : ( round( m.phase_1_current.value, 1),round( m.phase_2_current.value, 1),round( m.phase_3_current.value, 1) ),
                "meter_phase_p"            : ( int( m.phase_1_power.value ), int( m.phase_2_power.value ), int( m.phase_3_power.value ) ),
                "meter_power_tweaked"      : int( self.meter_power_tweaked ),
                "house_power"              : int( self.house_power ),
                "total_grid_port_power"    : int( self.total_grid_port_power ),
                "total_pv_power"           : int( router_total_pv_power ),
                "total_input_power"        : int( router_total_input_power ),
                "total_battery_power"      : int( router_total_battery_power ),
                "battery_max_charge_power" : int( router_battery_max_charge_power ),

                "mppt_power"               : mppt_power,
                "data_timestamp"           : m.last_transaction_timestamp,
            }
            self.mqtt.mqtt.publish( "nolog/pv/router_data", orjson.dumps( d ) )

        except Exception:
            log.exception("PowerManager coroutine:")

########################################################################################
#
#   FakeMeter callback before serving values to inverter
#
########################################################################################
def fakemeter_on_getvalues( self, fc_as_hex, address, count ):
    try:
        t = time.monotonic()
        # if self.key == "fake_meter_2":
            # log.info("%s %s %s", fc_as_hex, address, count)
        self.request_count += 1

        # Print message if inverter talks to us
        if self.last_query_time < t-2.0:
            log.info("FakeMeter %s: receiving requests", self.key )
        self.last_query_time = t

        if not self.data_timestamp:
            return False            # Not initialized yet

        # Publish requests/second statistics
        age = t - self.data_timestamp
        if (elapsed := self.stat_tick.ticked()) and self.mqtt_topic:
            self.mqtt.publish_value( self.mqtt_topic + "req_per_s", int(self.request_count/max(elapsed,1)) )
            self.request_count = 0

        # Do not serve stale data
        if self.is_online:
            # publish lag between real meter data and inverter query
            self.mqtt.publish_value( self.mqtt_topic + "lag", age, lambda x:round(x,2) )

            # do not serve stale data
            if age <= config.FAKE_METER_MAX_AGE:
                self.error_count = 0
                return True
            else:
                self.is_online = False
                if self.error_count < 100:
                    log.error( "FakeMeter %s: data is too old (%f seconds) [%d errors]", self.key, age, self.error_count )

        self.error_count += 1
        lm = self.solis.local_meter
        lm_age = t-lm.last_transaction_timestamp 
        if lm_age < config.FAKE_METER_MAX_AGE:
            self.active_power.value =  lm.active_power.value
            self.write_regs_to_context([ self.active_power ])
            if self.error_count < 100:
                log.error( "FakeMeter %s: using local meter data", self.key )
            return True

        if self.error_count < 100:
            log.error( "FakeMeter %s: local meter data is too old (%f seconds)", self.key, lm_age )

        return False

    except Exception:
        log.exception("fakemeter_on_getvalues")
        raise


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
    #                 log.info( "Remain in backup mode for %d s", timeout_blackout.remaining() )
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
class PSCtx:
    pass

async def inverter_powersave_coroutine( module_updated, first_start, self, solis ):
    power_reg = solis.rwr_power_on_off
    is_on = lambda: (power_reg.value == power_reg.value_on)
    chrono = Chrono()

    while not module_updated(): # Exit if this module was reloaded
        await solis.event_all.wait()
        self.mqtt.publish_value( "pv/emergency_stop", self.emergency_stop_button.value )
        if self.emergency_stop_button.value:
            if await power_reg.write_if_changed( power_reg.value_off ):
                log.info( "%s: Powering OFF (Emergency stop button)", solis.key )
            continue

        if not solis.is_online:
            continue
        try:
            # Initialize on/off counter on first load
            if first_start:
                solis._on_off_counter = BoundedCounter( 0, 100*is_on(), 100 )
                first_start = False

            # Auto on/off: turn it off at night when the battery is below specified SOC
            # so it doesn't keep draining it while doing nothing useful
            inverter_cfg = config.SOLIS_POWERSAVE_CONFIG[ solis.key ]
            counter = solis._on_off_counter
            elapsed = chrono.lap()
            
            reason = ""
            if inverter_cfg["MODE"] == "off":
                reason = "Turn off by config"
                counter.to_minimum()

            elif inverter_cfg["MODE"] == "on":
                reason = "Turn on by config"
                counter.to_maximum()

            elif self.bms_soc.age() < 120:   # we have received SOC
                ctx = PSCtx()
                ctx.mpptv = max( solis.mppt1_voltage.value, solis.mppt2_voltage.value )
                ctx.is_on = power_reg.value == power_reg.value_on
                ctx.soc   = self.bms_soc.value
                ctx.solis = solis
                ctx.self  = self
                reason, delta = inverter_cfg["FUNC"](ctx)
                counter.add( delta * elapsed )
                reason = "%s, MPPT %dV SOC %d%% house_power %dW pump %d" % ( reason, ctx.mpptv, ctx.soc, self.house_power, self.chauffage_pac_pompe.value )

            if counter.at_maximum():
                if await power_reg.write_if_changed( power_reg.value_on ):
                    log.info( "%s: Powering ON (%s)", solis.key, reason )
            elif counter.at_minimum():
                if await power_reg.write_if_changed( power_reg.value_off ):
                    log.info( "%s: Powering OFF (%s)", solis.key, reason )
            else:
                log.debug( "%s: powersave counter: %.1f", solis.key, counter.value )
                    
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
    bat_power_avg = { _.key: MovingAverageSeconds(10) for _ in self.inverters }
    tick = Metronome( 2 )
    # if first_start:
    #     await asyncio.sleep( 5 )
    fan_speed = { inverter.key: 100 for inverter in self.inverters }
    fan_timeout = { inverter.key: Timeout() for inverter in self.inverters }
    while not module_updated(): # Exit if this module was reloaded
        elapsed = await tick
        try:
            for solis in self.inverters:
                # run the fan if it is ON and HOT
                if solis.is_online and solis.rwr_power_on_off.value == solis.rwr_power_on_off.value_on:
                    # get desired fan speed from temperature (reactive) and battery power (proactive)
                    speed = max( 
                        config.FAN_SPEED[ "batp" ]( abs(solis.battery_power.value) ),
                        config.FAN_SPEED[ "temp" ]( solis.temperature.value ),
                    )

                    # attack/release to prevent spurious spin-up and delay fan spindown
                    prev = fan_speed[ solis.key ]
                    speed = clip( prev-config.FAN_SPEED["release"]*elapsed, speed, prev+config.FAN_SPEED["attack"]*elapsed )
                    speed = clip( 0, speed, 100 )
                else:
                    speed = 0
                fan_speed[ solis.key ] = speed

                # spindown timeout
                min_speed = config.FAN_SPEED["min_speed"]
                if speed >= min_speed:
                    fan_timeout[ solis.key ].reset( config.FAN_SPEED["stop_time"] )
                elif not fan_timeout[ solis.key ].expired():
                    speed = min_speed  # run at minimum speed for a while
                else:
                    speed = 0          # then stop

                self.mqtt.publish_value( "nolog/%sfan_speed" % solis.mqtt_topic, int(speed) )

        except (TimeoutError, ModbusException): pass
        except:
            log.exception("")
            await asyncio.sleep(5)

