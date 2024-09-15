#!/usr/bin/python
# -*- coding: utf-8 -*-

import time, asyncio, datetime, logging, collections, traceback, pymodbus
from pymodbus.exceptions import ModbusException
from asyncio.exceptions import TimeoutError

# Device wrappers and misc local libraries
import grugbus
from grugbus.devices import EVSE_ABB_Terra
from pv.mqtt_wrapper import MQTTWrapper
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
        mqtt.register_callbacks( self, "cmnd/" + mqtt_topic )

        # Modbus polling
        self.tick      = Metronome( config.POLL_PERIOD_EVSE )   # how often we poll it over modbus
        self.event_all = asyncio.Event()  # Fires when all registers are read, for slower processes
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

        # settings, DO NOT CHANGE as these are set in the ISO standard
        self.i_pause = 5.0          # current limit in pause mode
        self.i_start = 6.0          # minimum current limit for charge, can be set to below 6A if charge should pause when solar not available
        self.i_max   = 30.0         # maximum current limit

        # incremented/decremented depending on excess power to start/stop charge
        self.start_counter = BoundedCounter( 0, -60, 60 )   # charge starts when this counter reaches maximum
        self.stop_counter  = BoundedCounter( 0, -60, 60 )   # charge stops  when this counter reaches minimum

        # current limit that will be sent to the EVSE
        self.current_limit_counter = BoundedCounter( self.i_start, self.i_start, self.i_max, round )

        #   This setting deserves a comment... This is the time interval between current_limit commands to the EVSE.
        #   After each command:
        #       The car's charger takes 2-3s to react, but we don't know yet if the charger will decide to use all of the allowed power, or only some.
        #       Then the solar inverter adjusts its output power, which takes another 1-2s. 
        #   During all the above, excess PV power calculated by the Router class is not really valid, and should not be used to trigger some other loads
        #   which would result in "overbooking" of PV power.
        #   In addition, excess PV power needs to be smoothed (see Router class) to avoid freaking out every time a motor is started in the house
        #   and causes a short power spike. Say over Ts seconds. 
        #   Excess power also needs to settle before it can be used to calculate a new current_limit command.
        #   All this means it will be quite slow. Issue a command, ignore excess power during about 4 seconds, then smooth it for 3 seconds, get a new value, update.
        #   The length of the smoothing is in the deque in Router() class.
        self.command_interval          = Timeout( 10, expired=True )
        self.command_interval_small    = Timeout( 10, expired=True )
        self.resend_current_limit_tick = Metronome( 10 )

        # forced charge mdoe
        self.force_charge_i   = 6         # guarantee this charge current
        self.force_charge_kWh  = 0       # until this energy has been delivered
        self.p_threshold_start = 1400   # minimum excess power to start charging

        # dead band for power routing
        self.p_dead_band = 0.5 * 240

        # state variables
        self.target_power   = None
        self.previous_delta = 0
        self.last_call      = Chrono()

    async def stop_charge( self, print_log=True ):
        if self.is_charging_unpaused():
            log.info("EVSE: stop charge")
            self.local_meter.tick.set( config.POLL_PERIOD_EVSE_METER[0]* 10 )   # less MQTT traffic when not charging
        self.target_power = None
        self.start_counter.to_minimum() # reset counters 
        self.stop_counter.to_minimum()
        await self.set_current_limit( self.i_pause )
        self.publish_target_power()

    async def start_charge( self ):
        log.info("EVSE: start charge")
        self.local_meter.tick.set( config.POLL_PERIOD_EVSE_METER[0] )
        self.target_power = None
        self.start_counter.to_maximum()
        self.stop_counter.to_maximum()
        await self.set_current_limit( self.i_start )
        self.publish_target_power()

    def publish_target_power( self ):
        t = self.target_power or (0,0)
        self.mqtt.publish_value( self.mqtt_topic+"target_power_min",  round(t[0],2) )
        self.mqtt.publish_value( self.mqtt_topic+"target_power_max",  round(t[1],2) )

    @MQTTWrapper.decorate_callback( "force_charge_i", int, range(6,30) )
    async def cb_force_charge_i( self, topic, payload, qos, properties ):
        self.force_charge_i   = payload

    @MQTTWrapper.decorate_callback( "force_charge_kWh", int, range(0,80) )
    async def cb_force_charge_kWh( self, topic, payload, qos, properties ):
        self.force_charge_kWh   = payload

    @MQTTWrapper.decorate_callback( "p_threshold_start", int, range(0,2000) )
    async def cb_p_threshold_start( self, topic, payload, qos, properties ):
        self.p_threshold_start   = payload

    async def read_coroutine( self ):
        mqtt = self.mqtt
        topic = self.mqtt_topic
        while True:
            try:
                await self.tick.wait()
                await self.read_regs( self.regs_to_read )
                for reg in self.regs_to_read:
                    mqtt.publish_reg( topic, reg )
                if config.LOG_MODBUS_REQUEST_TIME:
                    mqtt.publish_value( topic+"req_time", round( self.last_transaction_duration,2 ) )

            except (TimeoutError, ModbusException):
                await asyncio.sleep(1)

            except Exception:
                log.exception(self.key+":")
                # s = traceback.format_exc()
                # log.error(self.key+":"+s)
                # self.mqtt.mqtt.publish( "pv/exception", s )
                await asyncio.sleep(1)

            self.event_all.set()
            self.event_all.clear()

    #   Main power routing func
    #
    async def route( self, excess ):
        power       = self.local_meter.active_power.value
        voltage     = self.local_meter.voltage.value
        time_since_last = min( 1, self.last_call.lap() )

        # monitor excess power to decide if we can start charge
        # do it before checking the socket state so when we plug in, it starts immediately
        # if power is available
        if excess >= self.p_threshold_start: self.start_counter.add( time_since_last )
        else:                                self.start_counter.add( -time_since_last )

        # if EV is not plugged, or everything is not initialized yet, do not charge
        if ((self.socket_state.value) != 0x111 or None in (power, voltage, excess) ):
            # set charge to paused, it will take many loops to increment this enough to start
            # print("noinit", self.socket_state.value, power, voltage, aexcess)
            return await self.stop_charge( )

        # TODO: do not trip breaker     mgr.meter.phase_1_current

        # now, EV is plugged in, but not necessarily authorized or willing to charge.
        # charge_state 500     : waiting for RFID authorization or other causes.
        #              1-2-300 : waiting for the car or current limit below 6A, not charging
        #              400     : charging
        # and... it's not necessary to check: if it wants to charge, it will do it automatically.

        # If self.force_charge_kWh is not zero, force charge until we put that amoHandle forced charge of at minimum self.force_charge_kWh
        if self.energy.value < self.force_charge_kWh:
            # tweak counters so charge never stops, triggers call to start_charge() if needed
            self.start_counter.to_maximum()
            self.stop_counter.to_maximum()
            # set lower bound for charge current to the minimum allowed by force charge
            # it is still allowed to use more power if there is more available
            self.current_limit_counter.minimum = self.force_charge_i
        else:
            # set lower bound at minimum allowed current
            self.current_limit_counter.minimum = self.i_start

        # Should we start charge ?
        if self.is_charging_paused():
            if self.start_counter.at_maximum():
                await self.start_charge()
            # return in all cases, since start_charge() writes the current limit via modbus
            # then the car takes 30s to start up, so we have nothing special to do now
            return

        # now we're in charge mode, not paused
        if power < 1200:
            # Current is low. This either means:
            # - Charge hasn't started yet, so we shouldn't issue new current adjustments and whack the current limit 
            # into the maximum and then charging would start with a big current spike.
            # - Or it is in the final balancing stage, and we want that to finish cleanly no matter what excess PV is.
            # In both cases, just do nothing, except prolong the timeout to delay the next current_limit update/
            self.command_interval.reset( 6 )
            return

        # Should we stop charge ? Do not move this check earlier as that would interrupt the final balancing stage
        if self.stop_counter.at_minimum():
            return await self.stop_charge()

        # Are we waiting for power to settle after a command?
        if self.target_power:   # this is the power we told the car to take
            mi, ma = self.target_power      # it's actually an [interval]
            # print( "target: %d<%d<%d" % (mi, power, ma))
            if mi <= power <= ma:
                # print("in range")
                self.target_power = None
                self.publish_target_power()       # publish it so the mqtt graph shows target has been reached
                self.command_interval.at_most(1)  # if timeout is still running, shorten it

        # Finally... adjust current.
        # Since the car imposes 1A steps, if we have a small excess that is lower than 1A then don't bother.
        delta_i = 0
        if abs(excess) > self.p_dead_band:
            delta_i     = round( excess * 0.004 )

        # stop charge if we're at minimum power and repeatedly try to go lower
        if delta_i < 0 and self.current_limit_counter.at_minimum():
            self.stop_counter.add( -time_since_last )
        else:
            self.stop_counter.add( time_since_last )

        # print("timeout remain", self.command_interval.remain())
        if not self.command_interval.expired():
            # we're not sure the previous command has been executed by the car, so wait.
            return

        # Now charge.
        # EVSE is slow, the car takes >2 seconds to respond to commands, then the inverter needs to adjust,
        # new value of excess power needs to stabilize, etc, before a new power setting can he issued.
        # This means we have to wait long enough between commands.

        # do we want to change the current setting? (this enforces min/max current bounds)
        change = await self.adjust_current_limit( delta_i, dry_run=True )

        # print( "excess %5d power %5d ilim %5d delta %s change %5d ssc %s" % (excess, power, self.rwr_current_limit.value, delta_i, change, self.stop_counter.value ))
        if not change:
            return

        # special case: do not bang it constantly with lots of small adjustments
        if abs(delta_i) <= 2 and not self.command_interval_small.expired():
            return

        # print( "execute", delta_i )
        self.previous_delta = delta_i = await self.adjust_current_limit( delta_i )
        self.command_interval.reset( 9 )        # wait time for next large update
        self.command_interval_small.reset( 6 )  # wait time for next small update

        # set target so we know it's executed
        if abs(delta_i) > 2:
            target_i = self.rwr_current_limit.value
            self.target_power = power+(delta_i-1.8)*voltage, power+(delta_i+1.8)*voltage
            self.publish_target_power()

    def is_charging_paused( self ):
        return self.rwr_current_limit.value < self.i_start

    def is_charging_unpaused( self ):
        return self.rwr_current_limit.value >= self.i_start

    async def adjust_current_limit( self, increment, dry_run=False ):
        # the car rounds current to the nearest integer number of amps, so 
        # the counter automatically rounds too, this avoids useless writes
        limit = self.current_limit_counter.pretend_add( increment )
        delta_i = limit - self.current_limit_counter.value # did it change?
        if not dry_run:
            await self.set_current_limit( limit )
        return delta_i

    async def set_current_limit( self, current_limit ):
        current_limit = round(current_limit)
        if self.resend_current_limit_tick.ticked():        
            # sometimes the EVSE forgets the current limit, so send it at regular intervals
            # even if it did not change
            await self.rwr_current_limit.write( current_limit )
            wrote = True
        else:
            # otherwise write only if changed
            wrote = await self.rwr_current_limit.write_if_changed( current_limit )

        if wrote:
            self.current_limit_counter.set( current_limit )
            # print( "write limit", current_limit )
            mqtt = self.mqtt
            topic = self.mqtt_topic
            mqtt.publish_reg( topic, self.rwr_current_limit )
            mqtt.publish_value( topic+"charging_unpaused"    ,  self.is_charging_unpaused() )
            if config.LOG_MODBUS_WRITE_REQUEST_TIME:
                self.mqtt.publish_value( self.mqtt_topic+"req_time", round( self.last_transaction_duration,2 ) )

