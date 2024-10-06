#!/usr/bin/python
# -*- coding: utf-8 -*-

import os, time, sys, serial, logging, logging.handlers, orjson, datetime
from path import Path

# This program is supposed to run on a potato (Allwinner H3 SoC) and uses async/await,
# so import the fast async library uvloop
import uvloop, asyncio
#aiohttp, aiohttp.web

# Modbus
import pymodbus
from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException
from asyncio.exceptions import TimeoutError, CancelledError

# Device wrappers and misc local libraries
from pv.mqtt_wrapper import MQTTWrapper, MQTTSetting, MQTTVariable
import grugbus
import pv.evse_abb_terra
import pv.reload
import config
from misc import *

"""
    python3.11
    pymodbus 3.7.x

Reminder:
git remote set-url origin https://<token>@github.com/peufeu2/GrugBus.git
"""

log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)

    # Solis S5 EH1P has several different behaviors to which we should adapt for better routing.
    #
    # 1) Feedback loop inoperative, PV controls the output
    #   Full export: (meter_export > 0) AND (Battery is full, battery_max_charge_current = 0)
    #   The inverver is exporting everything it can. Routing power will change meter_export but the inverter 
    #   won't react to that unless we draw too much, causing meter_export to become negative, at this point 
    #   it will retake control and draw power from the battery, exiting this mode.
    #   -> The router is actually in control of everything
    #   -> We can route as fast as possible, but keep meter_export > 0
    #
    # 1a) EPM Export power limit (per inverter setting)
    #   Feedback loop operating. Inverter controls export power.
    #   In this mode, the feedback loop is very quick: it reads the meter once per second and reacts almost
    #   immediately, taking more power from PV if available.
    #   -> Again, route as fast as possible, taking into account the inverter will react one second later.
    #   
    # 2) Inverter grid port is maxed out
    #   The inverter gets more power from PV than its grid port can export. This power can be used to charge 
    #   the battery, if needed. This mode is relevant for routing because while it would appear we can steal
    #   power from the battery (because we see it is charging), in reality we can't because the inverter 
    #   can't output more power on the grid port. So any extra load being switched on would draw from the grid.
    #   -> Detect this and avoid this mistake
    #   -> Inverter is maxed out, so the router is actually in control of everything
    #
    # 3) Battery DC-DC is operating
    #     Unless the battery DC-DC is maxed out, this corresponds to meter_export being near zero.
    #     When the inverter is working with the battery (doesn't matter if charging or discharging) the control 
    #   loop becomes much slower. A few seconds delay is inserted into the loop.


TEST_MODE = False


#################################################################################
#
#    Generic routable device
#
#################################################################################
class Routable():
    def __init__( self, router, key ):
        self.router = router
        self.key  = key
        self.mqtt = router.mqtt
        self.mqtt_topic = router.mqtt_topic + key + "/"
        self.reload_config()
        _reload_object_classes.append( self )

    def reload_config( self ):
        self.__dict__.update( config.ROUTER_CONFIG_DEFAULTS[ self.key ] | config.ROUTER_CONFIG[ self.router.active_config.value ].get( self.key, {} ) ) # load configuration

        # Priority of routable loads can be set via MQTT
        # MQTTVariable( mqtt_topic+"priority", self, "priority", float, None, priority )

    """
        Stop everything, called on program exit.
    """
    async def stop( self ):
        pass

    """
        Returns debugging string with state.
    """
    def dump( self ):
        pass

    """
        called on each update to let the device update its internal state.
        Returns None or False if everything is OK.
        Returns something if the device just changed its power, so readings will be fluctuating,
        which means the router should disable all routing, wait, and call again at the next
        iteration.
    """
    async def run( self ):
        return

    """
        How much power it is currently using. For example if the device is a heater with a
        thermostat, it can decide to stop on its own.
    """
    def get_power( self ):
        pass

    """
        Returns how much power this device can release immediately.
        The router puts this into the pool, to distribute among other devices
        according to priority.
    """
    def get_releaseable_power( self ):
        pass

    """( power in watts, pretend=True )
        Takes the requested absolute power

        Returns how much power it will take after the adjustment settles.

        Takes into account the min/max power of the device.
        For an on/off device, it either returns full power or zero.
        For an adjustable device, it takes power steps into account if the device has steps.

    """
    async def take_power( self, ctx ):
        pass

    """
        Called on each iteration after routing is done to publish
        stuff on MQTT if desired.
    """
    def publish( self ):
        pass

#################################################################################
#
#    Tasmota smartplug to switch heaters
#
#################################################################################
class TasmotaPlug( Routable ):

    def __init__( self, router, key ):
        super().__init__( router, key )
        self.current_power  = 0             # real mesurement
        self.power_measurements = collections.deque( maxlen=5 )
        self.last_power_when_on = self.estimated_power # remember how much it used when it was on, even if it turns off on its own
        self.is_on = False
        self.switch_timeout = Timeout()      # How long to wait after switching before switching again
        self.can_switch = False

        # We can also request it to post power with cmnd/plugs/tasmota_t2/Status 8
        # It is published in stat/plugs/tasmota_t2/STATUS8/StatusSNS/ENERGY/Power

        MQTTVariable( "tele/"+self.plug_topic+"LWT", self, "is_online", lambda s:s==b"Online", None, False )
        MQTTVariable( "tele/"+self.plug_topic+"SENSOR/ENERGY/Power", self, "_sensor_power", float, None, 0, self._mqtt_sensor_callback )
        MQTTVariable( "stat/"+self.plug_topic+"STATUS8/StatusSNS/ENERGY/Power", self, "_status_power", float, None, 0, self._mqtt_sensor_callback )

        # Request frequent power updates
        self.mqtt.publish( "cmnd/"+self.plug_topic+"PowerDelta", 101 )

    # get power reported by plug, and remember it
    async def _mqtt_sensor_callback( self, param ):
        log.info( "%s: is %s MQTT Power %f W" % (self.plug_topic, ('OFF','ON')[self.is_on], param.value ) )
        if self.is_on and param.value > self.min_power:    # remember how much it uses
            self.power_measurements.append( param.value )
            self.current_power = self.last_power_when_on = max( self.power_measurements ) # ignore low readings just after turning on

    def publish_power( self ):
        if not TEST_MODE:
            self.mqtt.publish_value( "cmnd/"+self.plug_topic+"Power", int(self.is_on) )

    async def on( self ):
        if not self.is_on:
            log.info( "Route ON  %s %s", self.name, self.plug_topic )
            self.is_on = True
            self.current_power = self.last_power_when_on
            self.switch_timeout.reset( config.router.plugs_min_on_time_s ) # stay on during minimum time
            self.publish_power()

    async def off( self ):
        if self.is_on:
            log.info( "Route OFF %s %s", self.name, self.plug_topic )
            self.is_on = False
            self.current_power = 0
            self.switch_timeout.reset( config.Router.plugs_min_off_time_s ) # stay off during minimum time
            self.publish_power()

    # turn it on or off
    async def onoff( self, on ):
        if on:  await self.on()
        else:   await self.off()

    # Routable class overrides

    async def stop( self ):
        await self.off()

    def dump( self ):
        return "%30s prio %d cur %5d %3s last %5d %s %s" % (self.name, self.priority, self.get_power(), ('OFF','ON')[self.is_on], self.last_power_when_on, ["","can_switch"][int(self.can_switch)], ["offline","online"][int(self.is_online.value)])

    async def run( self ):
        # prevent it from switching too frequently. This is put here and not
        # in take_power() because the timeout may expire between calls to take_power().
        self.can_switch = self.is_online.value and self.switch_timeout.expired()

    def get_power( self ):
        return self.current_power

    def get_releaseable_power( self ):
        return self.get_power()

    async def take_power( self, ctx ):
        if self.can_switch:
            if self.is_on:
                if ctx.power <= self.current_power - self.hysteresis:
                    ctx.changes.append( self.off )
                    return 0
            else:
                # It is off.
                if ctx.power >= self.last_power_when_on + self.hysteresis:
                    ctx.changes.append( self.on )
                    return self.last_power_when_on
        return self.get_power()

    # republish power commands

    def publish( self ):
        self.publish_power()

#################################################################################
#
#   This represents a slice of battery power, as a routable device.
#   By cutting battery power into slices, we can assign different priotities
#   to each slice.
#
#################################################################################
class Battery( Routable ):
    def __init__( self, router, key ):
        super().__init__( router, key )
        self.current_power = 0

    # Routable class overrides
    async def stop( self ):
        pass
    
    def dump( self ):
        return "%30s prio %d cur %5d max %5d" % (self.name, self.priority, self.get_power(), self.max_power_from_soc )

    def get_power( self ):
        return self.current_power

    def get_releaseable_power( self ):
        # special case for battery: we put all the battery power into excess calculation at the beginning,
        # so there's no need to add it again here.
        return 0

    async def take_power( self, ctx ):
        # in phase 2, take max charging power, to ensure
        # total excess is well into the negative when it needs to be
        p = interpolate( *self.interp, ctx.soc )
        self.max_power_from_soc = min( ctx.bp_max, p )
        self.current_power = min( ctx.power, self.max_power_from_soc )
        return self.max_power_from_soc


#################################################################################################
#
#   EVSE controller (the part that talks to the actual EVSE is in pv/evse_abb_terra.py)
#
#################################################################################################
class EVSEController( Routable ):
    def __init__( self, router, evse ):
        super().__init__( router, evse.key )
        self.evse        = evse
        self.local_meter = evse.local_meter
        self.is_online   = False

        #
        #   Parameters
        #   Note force charge and energy limit settings are reset once the car is unplugged.
        #
        MQTTSetting( self, "force_charge_minimum_A"     , int  , range( 6, 32 ) , 10 )  # guarantee this charge minimum current    
        MQTTSetting( self, "force_charge_until_kWh"     , int  , range( 0, 81 ) , 0  , self.setting_updated )  # until this energy has been delivered (0 to disable force charge)
        MQTTSetting( self, "stop_charge_after_kWh"      , int  , range( 0, 81 ) , 10 , self.setting_updated )

        # settings, DO NOT CHANGE as these are set in the ISO standard
        self.i_pause = 5.0          # current limit in pause mode
        self.i_start = 6.0          # minimum current limit for charge, can be set to below 6A if charge should pause when solar not available
        self.i_max   = 30.0         # maximum current limit

        # incremented/decremented depending on excess power to start/stop charge
        self.start_counter = BoundedCounter( 0, 50, 60 )   # charge starts when this counter reaches maximum
        self.stop_counter  = BoundedCounter( 0, -60, 60 )  # charge stops  when this counter reaches minimum
        self.integrator    = BoundedCounter( 0, -160, 160 )    # in Watts

        # current limit that will be sent to the EVSE, incremented and decremented depending on available power
        self.current_limit_bounds = BoundedCounter( self.i_start, self.i_start, self.i_max, round )

        # After issuing a command, we report power used as the value that was set until this expires,
        # then it reports real power from the meter
        self.power_report_timeout      = Timeout()
        self.power_report_value = 0

        #   After each command:
        #       The car's charger takes 2-3s to react, but we don't know yet if the charger will decide to use all of the allowed power, or only some.
        #       Then the solar inverter adjusts its output power, which takes another 1-2s. 
        #   During all the above, excess PV power calculated by the Router class includes power the car is about to take, 
        #   so it not be used to trigger some other loads which would result in "overbooking" of PV power.
        #   In addition, excess PV power needs to be smoothed (see Router class) to avoid freaking out every time a motor is started in the house.
        #   Excess power also needs to settle before it can be used to calculate a new current_limit command.
        #   All this means it will be quite slow.

        # Note the delay for all these timeouts is set later in the code
        self.command_interval          = Timeout() # different intervals depending on size of current_limit adjustment (see below)
        # self.settle_timeout            = Timeout() # different intervals depending on size of current_limit adjustment (see below)
        self.command_interval_small    = Timeout() # different intervals depending on size of current_limit adjustment (see below)
        self.up_timeout                = Timeout()      # after reducing current, wait before increasing it again

        self.unplug_timeout            = Timeout( 10)

        self.end_of_charge_timeout     = Timeout()
 
        self.last_call     = Chrono()
        self.car_ready = None    # if car is plugged in and ready
        self.soft_start_timestamp = 0                # set when car begins to draw current, for soft start

        # State of this softwate (not the charger)
        self.set_state( self.STATE_UNPLUGGED )

    STATE_UNPLUGGED = 0 # Car is not plugged
    STATE_PLUGGED   = 1 # Car is plugged, waiting for enough excess PV or force charge
    STATE_CHARGE_STARTED  = 2 # Charge command has been sent, but the car isn't drawing current yet.
    STATE_CHARGING = 3 # Current is flowing
    STATE_FINISHING = 4 # Final balancing stage, do not stop charge when in this state.
    STATE_FINISHED  = 5 # Energy limit reached. If energy limit is not set, this state is never reached.

    # sanitize settings
    async def setting_updated( self, setting=None ):
        stop = self.stop_charge_after_kWh
        force = self.force_charge_until_kWh
        if stop.value < force.value:
            if setting is force:    stop.value = force.value = max( stop.value, force.value )
            elif setting is stop:   stop.value = force.value = min( stop.value, force.value )
            stop.publish()
            force.publish()
        if setting in (force, stop):    # if it was updated, go back from finished to charging
            if self.state == STATE_FINISHED:
                self.set_state( self.STATE_UNPLUGGED )

    # Async stuff that can't be in constructor
    async def initialize( self ):
        await self.setting_updated()
        await self.pause_charge( self.STATE_UNPLUGGED )

    def is_charge_paused( self ):
        return self.evse.rwr_current_limit.value < self.i_start

    def is_charge_unpaused( self ):
        return self.evse.rwr_current_limit.value >= self.i_start

    async def pause_charge( self, state ):
        if self.is_charge_unpaused():
            log.info("EVSE: Pause charge")
            self.router.hair_trigger( 3 ) # take power released by the car immediately
        self.local_meter.tick.set( config.POLL_PERIOD_EVSE_METER* 10 )   # less MQTT traffic when not charging
        self.start_counter.to_minimum() # reset counters 
        self.stop_counter.to_minimum()
        self.set_state( state )
        await self.evse.set_current_limit( self.i_pause )

    async def resume_charge( self, state ):
        if self.is_charge_paused():
            log.info("EVSE: Resume charge")
            self.end_of_charge_timeout.reset( self.end_of_charge_timeout_s )
        self.local_meter.tick.set( config.POLL_PERIOD_EVSE_METER ) # poll meter more often
        self.start_counter.to_maximum()
        self.stop_counter.to_maximum()
        self.integrator.set(0)
        self.set_state( state )
        await self.evse.set_current_limit( self.i_start )

    def advance_state( self, state ):
        self.set_state( max( self.state, state ) )

    def set_state( self, state ):
        self.state = state
        self.mqtt.publish_value( self.mqtt_topic+"state",  state )

    #
    #   Routable overrides
    #
    async def stop( self ):
        await self.pause_charge( self.STATE_UNPLUGGED )

    def dump( self ):
        return "%30s prio %d cur %5d ILim %5f state %d ss %d/%d" % (self.name, self.priority, self.get_power(), self.evse.rwr_current_limit.value, self.state, self.start_counter.value, self.stop_counter.value  )

    async def run( self ):
        self.is_online = self.local_meter.is_online and self.evse.is_online
        if not self.is_online or self.is_charge_paused():
            return     # we're inactive, do not block routing

        # Enable routing?
        if not self.is_meter_stable():
            return "wait meter"

    def is_meter_stable( self ):
        h = self.local_meter.power_history
        return max(h) - min(h) < self.stability_threshold_W        

    def get_power( self ):
        # take recent command into account, in case we just increased power
        if not self.is_online:
            return 0
        if self.power_report_timeout.expired():
            return self.local_meter.active_power.value
        else:
            return self._get_power()

    # This function is overwritten when changing the current limit
    def _get_power( self ):
        return self.local_meter.active_power.value

    def get_releaseable_power( self ):
        return self.get_power()

    async def take_power( self, ctx ):
        # First take our minimum power (if any)
        min_ev_power = min( ctx.power, self.high_priority_power )
        remain = ctx.power - min_ev_power

        # Leave some for the battery
        bat     = min( remain, ctx.bp_max, interpolate( *self.battery_interp, ctx.soc ))

        # Take everything else
        avail_power = ctx.power - bat

        p = await self._take_power( avail_power, ctx )
        if p == None:
            return self.get_power()
        return p

    async def _take_power( self, avail_power, ctx ):
        time_since_last_call = min( 1, self.last_call.lap() )
        # print( "evse: start %-2d stop %-2d power %d " % (self.start_counter.value, self.stop_counter.value, self.local_meter.active_power.value) )

        # TODO: do not trip breaker     mgr.meter.phase_1_current

        #   Timer to Start Charge
        #
        if self.is_online and self.evse.energy.value < self.force_charge_until_kWh.value:   # Force charge?
            self.start_counter.to_maximum() # tweak counters so charge starts immediately and does not stop
            self.stop_counter.to_maximum()
            # set lower bound for charge current to the minimum allowed by force charge
            # it is still allowed to use more power if there is more available
            self.current_limit_bounds.set_minimum( self.force_charge_minimum_A.value )
        else:
            # monitor excess power to decide if we can start charge
            # do it before checking the socket state so when we plug in, it starts immediately
            # if power is available
            self.start_counter.addsub( avail_power >= self.start_excess_threshold_W, time_since_last_call )
            self.current_limit_bounds.set_minimum( self.i_start )   # set lower bound at minimum allowed current

        # Are we connected? If RS485 fails, EVSE will timeout and stop charge.
        if not self.is_online:
            return

        #   Plug/Unplug
        #
        self.car_ready = (self.evse.socket_state.value == 0x111)  # EV plugged and ready to charge
        if not self.car_ready:          # EV is not plugged in.
            if self.unplug_timeout.expired_once():
                log.info("EVSE: EV unplugged. Reset settings.")
                self.force_charge_until_kWh.set( 0 ) # reset it after unplugging car
                self.stop_charge_after_kWh .set( 0 )
                await self.pause_charge( self.STATE_UNPLUGGED )
            return
        
        self.advance_state( self.STATE_PLUGGED )
        self.unplug_timeout.reset( 3 )  # sometimes the EVSE crashes or reports "not ready" for one second

        # now, EV is plugged in, but not necessarily authorized or willing to charge.
        # charge_state 500     : waiting for RFID authorization or other causes.
        #              1-2-300 : waiting for the car or current limit below 6A, not charging
        #              400     : charging
        # and... it's not necessary to check: if it wants to charge, it will do it automatically.

        # Do not interrupt the final balancing stage
        if self.state >= self.STATE_FINISHING:
            return 

        # Energy limit?
        if (v := self.stop_charge_after_kWh.value) and self.evse.energy.value >= v:
            await self.pause_charge( self.STATE_FINISHED )
            return

        # Should we start charge ?
        if self.is_charge_paused():
            if self.start_counter.at_maximum():     # this is incremented by the router if excess power is available
                await self.resume_charge( max( self.state, self.STATE_CHARGE_STARTED ))          # also resets end_of_charge_timeout
            return # return in all cases, since the car takes 30s to start up,  we have nothing special to do now

        # now we're in charge mode, not paused
        ev_power   = self.local_meter.active_power.value
        voltage = self.local_meter.voltage.value            # Use car's actual power use
        if ev_power < self.charge_detect_threshold_W:
            # Current is low. This either means:
            # - Charge hasn't started yet, so we shouldn't issue new current adjustments and whack the current limit 
            # into the maximum and then charging would start with a big current spike.
            # - Or it is in the final balancing stage, and we want that to finish cleanly no matter what excess PV is.
            # In both cases, just do nothing, except prolong the timeout to delay the next current_limit update
            if self.end_of_charge_timeout.expired():
                # Low current for a long time means the charge is finishing
                # Note unless we specify a maximum energy, it is not possible to know
                # if charge is finished. The car sometimes wakes up to draw a bit of power.
                # so we will stay in STATE_FINISHING until unplugged.
                self.advance_state( self.STATE_FINISHING )
            # ready to soft-start when the car begins charging
            self.command_interval.reset( 5 )
            self.soft_start_timestamp = time.monotonic()
            return

        self.end_of_charge_timeout.reset( self.end_of_charge_timeout_s )

        # Should we stop charge ? Do not move this check earlier as that would interrupt the final balancing stage
        if self.stop_counter.at_minimum():
            await self.pause_charge( self.STATE_PLUGGED )
            return 

        if self.state == self.STATE_CHARGE_STARTED:
            self.set_state( self.STATE_CHARGING )
            self.router.hair_trigger( 0.5 ) # shut down low priority loads immediately

        # Soft start
        self.current_limit_bounds.set_maximum( max( self.i_start, min( self.i_max, time.monotonic() - self.soft_start_timestamp )))

        # Finally... adjust current.
        # Since the car imposes 1A steps, don't bother with changes smaller than this.
        excess = avail_power - ev_power
        if abs(excess) < self.dead_band_W:
            self.integrator.value = 0.
            return

        # calculate new limit
        # TODO: redo integrator using local meter instead
        cur_limit = self.evse.rwr_current_limit.value
        self.integrator.add( excess * self.control_gain_i * time_since_last_call )
        new_limit = (avail_power + self.integrator.value) * self.control_gain_p / voltage
        self.stop_counter.addsub( new_limit >= self.i_start, time_since_last_call ) # stop charge if we're at minimum power and repeatedly try to go lower
        new_limit = self.current_limit_bounds.clip( new_limit )                # clip it so we keep charging until stop_counter says we stop
        delta_i = new_limit - cur_limit
        new_power = ev_power + delta_i * voltage

        # self.mqtt.publish_value( self.mqtt_topic+"command_interval_small",  round(self.command_interval_small.remain(),1) )
        # self.mqtt.publish_value( self.mqtt_topic+"command_interval",        round(self.command_interval.remain(),1) )

        log.debug( "power %4d voltage %4d current %5.02f integ %.02f limit %d -> %d timeouts %.02f %.02f", ev_power, voltage, ev_power/voltage, self.integrator.value, cur_limit, new_limit, self.command_interval.remain(), self.command_interval_small.remain() )

        # no change
        if not delta_i:
            return

        # do not constantly send small changes
        if abs( delta_i ) <= self.small_current_step_A:
            if not self.command_interval_small.expired():
                return

        # after reducing current, wait until bringing it back up
        if delta_i>0 and not self.up_timeout.expired():
            return

        # wait after command before sending a new one
        if not self.command_interval.expired():
            return

        # TODO: remove. This moved into router code.
        # If a change is needed, it means power is changing, and that means we should wait for readings 
        # to stabilize.  This avoids sending a small change at the beginning of a step, then having to wait for
        # the car to update to proces the rest of the step.
        # if not self.settle_timeout.expiry:      # Timeout is not set
            # self.settle_timeout.reset( self.settle_timeout_s )   # Set timeout, when it expires we end up at the next line

        # if not self.settle_timeout.expired():
            # return

        async def execute():
            # after reducing current, wait until bringing it back up
            if delta_i<0:
                self.up_timeout.reset( self.up_timeout_s )

            # reset integrator on large steps
            if abs( delta_i ) > self.small_current_step_A:
                self.integrator.value = 0.

            # Execute current limit change
            await self.evse.set_current_limit( new_limit )
            self.command_interval.reset( self.command_interval_s )
            self.command_interval_small.reset( self.command_interval_small_s )  # prevent frequent small updates

            # alter power reporting function
            self.power_report_timeout.reset( self.power_report_timeout_s )
            # if delta_i>0:
            self._get_power = lambda: max( new_power, self.local_meter.active_power.value ) 
            # else:
                # self._get_power = lambda: max( new_power, self.local_meter.active_power.value )

        ctx.changes.append( execute )
        return max( new_power, self.local_meter.active_power.value )    # reports new power to router, even if it discards the changes

    def publish( self ):
        pass


class RouterCtx:
    def __init__( self ):
        self.changes = []

class Router( ):
    def __init__( self, mgr, mqtt, mqtt_topic ):

        _reload_object_classes.append( self )

        self.mgr         = mgr
        self.mqtt        = mqtt
        self.mqtt_topic  = mqtt_topic

        # complete configurations (in config.py) can be loaded by MQTT command
        # individual settings are not available, as that would complicate the HA GUI too much
        MQTTSetting( self, "active_config"      , str, False, "default", self.mqtt_config_updated_callback ) #

        self.evse = EVSEController( self, mgr.evse )
        self.battery = Battery( self, key="bat" )
        self.devices = [ 
            TasmotaPlug( self, key="tasmota_t4" ),
            TasmotaPlug( self, key="tasmota_t2" ),
            TasmotaPlug( self, key="tasmota_t1" ),
            self.battery,
            self.evse,
        ] 

        # This also sorts devices by priority
        self.reload_config()

        # queues to smooth measured power
        self.smooth_export = MovingAverageSeconds( 1 )
        self.smooth_bp     = MovingAverageSeconds( 1 )
        self.battery_active_avg = MovingAverageSeconds( 20 )
        self.battery_full_avg   = MovingAverageSeconds( 20 )

        # When it wants to make a change, start the timeout.
        # Check again when it expires, and if we still want to make the change, then
        # do it. This avoids reacting on spikes and other noise.
        self.confirm_change_counter = BoundedCounter( 0, 0, 1.5 )
        self.last_call = Chrono()
        self.hair_trigger_timeout = Timeout( expired = True )

        MQTTSetting( self, "offset", float, None, 0 )

        # How long to disable routing changes after a plug switches
        # (to let power measurement settle).
        # (towel dryer takes ~1s to wake up)
        # MQTTSetting( self, "plugs_holdoff_s", float, lambda x: (0<=x<=2), 1.5 )

        self.initialized = False

    # callback when config module is reloaded
    def reload_config( self ):
        log.info( "Router: set config %s", self.active_config.value )
        for device in self.devices: device.reload_config()

        self.devices = sorted( self.devices, key=lambda d:-d.priority )

    # MQTT message received on config topic
    async def mqtt_config_updated_callback( self, param ):
        self.reload_config()

    async def stop( self ):
        # await mgr.evse.pause_charge()
        for d in self.devices:
            await d.stop()

    # Puts the router on a hair trigger for the specified duration in seconds
    def hair_trigger( self, duration ):
        self.hair_trigger_timeout.reset( duration )

    async def route( self ):
        mgr = self.mgr
        time_since_last_call = min( 1, self.last_call.lap() )

        # Smartmeter power alone is not sufficient: at night when running on battery it will fluctuate
        # around zero but with spikes in import/export and we don't want that to trigger loads.
        # Likewise when charging during the day, if a load turns on it can steal all the power 
        # from battery charging. We don't want that. Solution:
        # The inverter already does all the work: excess production is (meter export) + (solar battery charging power)
        # so, by using battery charging power, this behaves correctly in all cases.

        # Get battery info
        bp_proxy  = mgr.total_input_power  # Battery charging power (fast proxy from smartmeter). Positive if charging, negative if discharging.
        soc = mgr.bms_soc.value            # battery SOC, 0-100

        # We need to know if the inverter will actually use the power we allocate to battery charging.
        # If battery_max_charge_power == 0, then we're sure it won't charge and can use all the power.
        # But when battery_max_charge_power > 0 and soc < 100, sometimes it doesn't charge, and exports instead.
        # So we detect if the inverter is charging. "bat_full" variable really means "inverter doesn't want to charge it"
        bat_active  = (self.battery_active_avg.append( config.Router.battery_active( mgr )) or 1) > config.Router.battery_active_threshold

        # remove battery power measurement error when battery is inactive
        if not bat_active:
            bp_proxy = 0    

        bat_full = (self.battery_full_avg.append( config.Router.battery_full( mgr, bat_active )) or 0) > config.Router.battery_full_threshold

        # mgr.battery_max_charge_power is the maximum charging power the battery can take, as determined by inverter, according to BMS info
        # bp_max is the max power the inverter will actually charge at, it feels like it
        if bat_full:
            bp_max  = 0
        else:
            bp_max = mgr.battery_max_charge_power

        # Get export power from smartmeter, set it to positive when exporting (invert sign), makes the algo below easier to understand.
        # Also smooth it, to avoid jerky behavior on power spikes
        # export_avg = self.smooth_export.append( -mgr.meter_power_tweaked )
        # bp_avg     = self.smooth_bp    .append( bp_proxy )

        # # Wait for moving average to fill
        # if not (self.smooth_export.is_full and self.smooth_bp.is_full):
        #     return

        # TODO: check if export smoothing is necessary, holdoff time should be enough
        export_avg = -mgr.meter_power_tweaked
        bp_avg     = bp_proxy

        # compute excess power, including battery power
        # and tweak it a bit to keep a little bit of extra power
        excess_avg = self.offset.value + export_avg + bp_avg - config.Router.p_export_target( soc )

        # Hack: If EV charging has begun, pretend we have more SOC than we have to avoid start/stop cycles
        if self.evse.is_charge_unpaused():
            soc += 5

        #
        #   Begin power routing
        #

        # update devices internal state
        wait = []
        for device in self.devices:
            if w:= await device.run():
                wait.append( device.name + ": " + w )

        # calculate excess power as if all routable devices were off, to let
        # higher priority devices take that power from lower priority devices
        excess = excess_avg
        log.debug( "excess %5d bp %5d %s %s", excess, bp_avg, "prep", datetime.datetime.now().isoformat())
        for device in self.devices:
            excess += device.get_releaseable_power( )

        # create routing context
        ctx = RouterCtx()
        ctx.soc    = soc
        ctx.power  = excess_avg
        ctx.total  = excess_avg
        ctx.bp_max = bp_max

        # avoid reacting on spikes: this requires several iterations wanting to 
        # change something before we commit to it

        # Scan from highest to lowest priority and let devices take power calculated above.
        log.debug( "%5d %s %s", ctx.power, "start", self.confirm_change_counter.value )
        for device in self.devices:
            p = await device.take_power( ctx )
            log.debug( "%-6d take %5d for %s", ctx.power, p, device.dump())
            ctx.power -= p

        log.debug( "%5d %s", ctx.power, "end")

        if wait:
            log.debug("waitlist %s", wait)

        if ctx.changes: # execute changes if confirmed
            self.confirm_change_counter.add( time_since_last_call )
            # if one device is in the process of switching or measuring power, wait until finished
            # except if hair trigger mode is activated
            if (self.confirm_change_counter.at_maximum() and not wait) or (not self.hair_trigger_timeout.expired()):
                log.debug("execute")
                for func in ctx.changes:
                    await func()
                self.confirm_change_counter.to_minimum()
            else:
                log.debug( "confirm_change_counter %.02f/%.02f", self.confirm_change_counter.value, self.confirm_change_counter.maximum )
        else:
            log.debug( "no change" )
            self.confirm_change_counter.to_minimum()

        # publish results
        for device in self.devices:
            device.publish()



# async def test_route( self, mgr ):

#     # power = total PV + grid export
#     for power in range( 100, 9000, 100 ):

#         # At the beginning, excess should contain either:
#         #   - grid export + total battery charging power
#         #   - or total PV power
#         #   - or total estimated possible PV power (using maximum of all strings)

#         bp_max = 5000
#         soc = 90
#         for battery_slice in self.devices_battery:
#             bp_max = battery_slice.set_power( soc, bp_max )

#         # simulate main meter accounting for loads that are on:
#         for device in self.devices:
#             if not isinstance( device, Battery ):
#                 power -= device.current_power

#         excess = power

#         for device in self.devices:
#             e = excess
#             excess += device.take_power( 0, do_it=False )
#             # print( "1.", device.mqtt_topic, device.get_power(), e, "->", excess )

#         #   Scan from highest to lowest priority.
#         #   Give available excess power to each device, in order.
#         print( "%5d %s" % (excess, "start") )
#         for device in reversed( self.devices ):
#             e = excess
#             excess = device.take_power( excess, do_it=True )
#             print( "%5d %s" % (e, device.dump()) )
#         print( "%5d %s" % (excess, "end") )


#         print()

#     raise CancelledError

# setup list of classes to reload once on first module import
if not hasattr( sys.modules[__name__], "_reload_object_classes" ):
    _reload_object_classes = []

# reload classes
def hack_reload_classes( ):
    for obj in _reload_object_classes:
        prev_module_name = obj.__class__.__module__
        if prev_module_name in pv.reload.module_names_to_reload:
            print( "Install new class for", prev_module_name, obj )
            mod = sys.modules.get( prev_module_name )
            newclass = getattr( mod, obj.__class__.__name__ )
            obj.__class__ = newclass

def on_module_unload():
    pass

#
#   Coroutine launched from pv_router.py and reloaded with module
#
async def route_coroutine( module_updated, first_start, mgr ):
    try:
        # reload classes
        # if not first_start:
            # mgr.router.__class__ = Router
            # mgr.router.evse.__class__ = EVSEController

        # wait for startup transient to pass
        # await asyncio.sleep(5)
        await mgr.event_power.wait()
        log.info("Routing enabled.")
        # await mgr.evse.initialize()

        while not module_updated():
            await mgr.event_power.wait()                
            try:
                await mgr.router.route()

            except Exception:
                log.exception("Router:")
                await asyncio.sleep(1)
    finally:
        if not module_updated():
            log.info("Router: STOP")
            await mgr.router.stop()
