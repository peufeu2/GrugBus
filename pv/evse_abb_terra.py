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

        #
        #   Parameters
        #

        # Forced charge mode. Note these are reset once the car is unplugged.
        self.start_excess_threshold = 1400    # minimum excess power to start charging, unless overriden by:
        self.force_charge_minimum_W = 1500 # guarantee this charge minimum current
        self.force_charge_until_kWh = 0 # until this energy has been delivered (0 to disable force charge)

        # dead band for power routing
        self.p_dead_band = 0.5 * 240

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

        # settings, DO NOT CHANGE as these are set in the ISO standard
        self.i_pause = 5.0          # current limit in pause mode
        self.i_start = 6.0          # minimum current limit for charge, can be set to below 6A if charge should pause when solar not available
        self.i_max   = 30.0         # maximum current limit

        # incremented/decremented depending on excess power to start/stop charge
        self.start_counter = BoundedCounter( 0, -60, 60 )   # charge starts when this counter reaches maximum
        self.stop_counter  = BoundedCounter( 0, -60, 60 )   # charge stops  when this counter reaches minimum

        # current limit that will be sent to the EVSE, incremented and decremented depending on available power
        self.current_limit_counter = BoundedCounter( self.i_start, self.i_start, self.i_max, round )

        #   After each command:
        #       The car's charger takes 2-3s to react, but we don't know yet if the charger will decide to use all of the allowed power, or only some.
        #       Then the solar inverter adjusts its output power, which takes another 1-2s. 
        #   During all the above, excess PV power calculated by the Router class includes power the car is about to take, 
        #   so it not be used to trigger some other loads which would result in "overbooking" of PV power.
        #   In addition, excess PV power needs to be smoothed (see Router class) to avoid freaking out every time a motor is started in the house.
        #   Excess power also needs to settle before it can be used to calculate a new current_limit command.
        #   All this means it will be quite slow.
        self.command_interval          = Timeout( 10, expired=True ) # different intervals depending on size of current_limit adjustment (see below)
        self.command_interval_small    = Timeout( 10, expired=True ) # different intervals depending on size of current_limit adjustment (see below)
        self.resend_current_limit_tick = Metronome( 10 )    # sometimes the EVSE forgets the setting, got to send it once in a while
        self.last_call     = Chrono()
        self.charge_active = False
        self.initialized   = False
        
    async def pause_charge( self, print_log=True ):
        if self.is_charge_unpaused():
            log.info("EVSE: Pause charge")
        self.local_meter.tick.set( config.POLL_PERIOD_EVSE_METER[0]* 10 )   # less MQTT traffic when not charging
        self.target_power = None
        self.previous_delta = 0
        self.start_counter.to_minimum() # reset counters 
        self.stop_counter.to_minimum()
        self.publish_target_power()
        await self.set_current_limit( self.i_pause )

    async def resume_charge( self ):
        if self.is_charge_paused():
            log.info("EVSE: Resume charge")
        self.local_meter.tick.set( config.POLL_PERIOD_EVSE_METER[0] ) # poll meter more often
        self.target_power = None
        self.previous_delta = 0
        self.start_counter.to_maximum()
        self.stop_counter.to_maximum()
        self.publish_target_power()
        await self.set_current_limit( self.i_start )

    async def start_charge( self ):
        if not self.charge_active:
            log.info("EVSE: Start charge")
            self.charge_active = True
        await self.resume_charge()

    async def stop_charge( self ):
        if self.charge_active or not self.initialized: # session finished, reset force charge
            log.info("EVSE: Stop charge")
            await self.pause_charge()
            self.force_charge_until_kWh = 0 # reset it after finishing charge
            self.charge_active = False
            self.initialized   = True

    def publish_target_power( self ):
        t = self.target_power or (0,0)
        self.mqtt.publish_value( self.mqtt_topic+"target_power_min",  round(t[0],2) )
        self.mqtt.publish_value( self.mqtt_topic+"target_power_max",  round(t[1],2) )

    @MQTTWrapper.decorate_callback( "force_charge_minimum_W", int, range(6,30) )
    async def cb_force_charge_minimum_W( self, topic, payload, qos, properties ):
        self.force_charge_minimum_W   = payload

    @MQTTWrapper.decorate_callback( "force_charge_until_kWh", int, range(0,80) )
    async def cb_force_charge_until_kWh( self, topic, payload, qos, properties ):
        self.force_charge_until_kWh   = payload

    @MQTTWrapper.decorate_callback( "start_excess_threshold", int, range(0,2000) )
    async def cb_start_excess_threshold( self, topic, payload, qos, properties ):
        self.start_excess_threshold   = payload

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

                # publish force charge info                
                mqtt.publish_value( topic+"charge_active"         , self.charge_active )
                mqtt.publish_value( topic+"force_charge_minimum_W", self.force_charge_minimum_W )
                mqtt.publish_value( topic+"force_charge_until_kWh", self.force_charge_until_kWh       )

                if config.LOG_MODBUS_REQUEST_TIME:
                    self.publish_modbus_timings()

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
        time_since_last = min( 1, self.last_call.lap() )

        # monitor excess power to decide if we can start charge
        # do it before checking the socket state so when we plug in, it starts immediately
        # if power is available
        if excess >= self.start_excess_threshold: self.start_counter.add( time_since_last )
        else:                                     self.start_counter.add( -time_since_last )

        # if EV is not plugged, or everything is not initialized yet, do not charge
        if (self.socket_state.value) != 0x111 or not self.initialized:
            # set charge to paused, it will take many loops to increment this enough to start
            # print("noinit", self.socket_state.value, power, voltage, aexcess)
            return await self.stop_charge( )

        # TODO: do not trip breaker     mgr.meter.phase_1_current

        # now, EV is plugged in, but not necessarily authorized or willing to charge.
        # charge_state 500     : waiting for RFID authorization or other causes.
        #              1-2-300 : waiting for the car or current limit below 6A, not charging
        #              400     : charging
        # and... it's not necessary to check: if it wants to charge, it will do it automatically.

        # If self.force_charge_until_kWh is not zero, force charge until we put that amoHandle forced charge of at minimum self.force_charge_until_kWh
        if self.energy.value < self.force_charge_until_kWh:
            # tweak counters so charge never stops, triggers call to resume_charge() if needed
            self.start_counter.to_maximum()
            self.stop_counter.to_maximum()
            # set lower bound for charge current to the minimum allowed by force charge
            # it is still allowed to use more power if there is more available
            self.current_limit_counter.minimum = self.force_charge_minimum_W
        else:
            # set lower bound at minimum allowed current
            self.current_limit_counter.minimum = self.i_start

        # Should we start charge ?
        if self.is_charge_paused():
            if self.start_counter.at_maximum():
                if not self.charge_active:  await self.start_charge()
                else:                       await self.resume_charge()
            # return in all cases, since the car takes 30s to start up, so we have nothing special to do now
            return

        # now we're in charge mode, not paused
        power = self.local_meter.active_power.value
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
            return await self.pause_charge()

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
            voltage     = self.local_meter.voltage.value
            self.target_power = power+(delta_i-1.8)*voltage, power+(delta_i+1.8)*voltage
            self.publish_target_power()

    def is_charge_paused( self ):
        return self.rwr_current_limit.value < self.i_start

    def is_charge_unpaused( self ):
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
            if config.LOG_MODBUS_WRITE_REQUEST_TIME:
                self.publish_modbus_timings()

