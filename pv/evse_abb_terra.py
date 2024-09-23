#!/usr/bin/python
# -*- coding: utf-8 -*-

import time, asyncio, datetime, logging, collections, traceback, pymodbus, sys
from pymodbus.exceptions import ModbusException
from asyncio.exceptions import TimeoutError

# Device wrappers and misc local libraries
import grugbus
from grugbus.devices import EVSE_ABB_Terra
from pv.mqtt_wrapper import MQTTWrapper, MQTTSetting
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

        #
        #   Parameters
        #   Note force charge and energy limit settings are reset once the car is unplugged.
        #
        MQTTSetting( self, "start_excess_threshold_W"   , int  , range( 0, 2001 ) , 1400 )  # minimum excess power to start charging, unless overriden by:    
        MQTTSetting( self, "charge_detect_threshold_W"  , int  , range( 0, 2001 ) , 1200 )  # minimum excess power to start charging, unless overriden by:    
        MQTTSetting( self, "force_charge_minimum_A"     , int  , range( 0, 32 )   , 10   )  # guarantee this charge minimum current    
        MQTTSetting( self, "force_charge_until_kWh"     , int  , range( 0, 81 )   , 0    )  # until this energy has been delivered (0 to disable force charge)
        MQTTSetting( self, "stop_charge_after_kWh"      , int  , range( 0, 81 )   , 6    )
        MQTTSetting( self, "offset"                     , int  , range( -10000, 10000 )   , 0    )
        MQTTSetting( self, "settle_timeout_ms"          , int  , range( 0, 10001 ),  250 )
        MQTTSetting( self, "command_interval_ms"        , int  , range( 0, 10001 ),  500 )
        MQTTSetting( self, "command_interval_small_ms"  , int  , range( 0, 10001 ), 9000 )
        MQTTSetting( self, "dead_band_W"                , int  , range( 0, 501 ),  0.5*240 )        # dead band for power routing
        MQTTSetting( self, "stability_threshold_W"      , int  , range( 50, 201 ),  150 )
        MQTTSetting( self, "small_current_step_A"       , int  , ( 1, 2 ),  1 )
        MQTTSetting( self, "control_gain"               , float, (lambda x: 0.1 <= x <= 0.99),  0.96 )

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
        self.start_counter = BoundedCounter( 0, 50, 60 )   # charge starts when this counter reaches maximum
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
        self.settle_timeout            = Timeout( 2, expired=True ) # different intervals depending on size of current_limit adjustment (see below)
        self.command_interval_small    = Timeout( 10, expired=True ) # different intervals depending on size of current_limit adjustment (see below)
        self.resend_current_limit_tick = Metronome( 10 )    # sometimes the EVSE forgets the setting, got to send it once in a while

        self._last_call     = Chrono()
        self._prev_car_ready = self.car_ready = None    # if car is plugged in and ready
        self._begin_charge_timestamp = 0                # set when car begins to draw current, for soft start

    # Async stuff that can't be in constructor
    async def initialize( self ):
        await self.pause_charge()

    def is_charge_paused( self ):
        return self.rwr_current_limit.value < self.i_start

    def is_charge_unpaused( self ):
        return self.rwr_current_limit.value >= self.i_start

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

    def publish_target_power( self ):
        t = self.target_power or (0,0)
        self.mqtt.publish_value( self.mqtt_topic+"target_power_min",  round(t[0],2) )
        self.mqtt.publish_value( self.mqtt_topic+"target_power_max",  round(t[1],2) )

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
        time_since_last_call = min( 1, self._last_call.lap() )

        excess += self.offset.value
        sys.stdout.write( "\revse: exc %-4d start %-2d stop %-2d power %d " % (excess, self.start_counter.value, self.stop_counter.value, self.local_meter.active_power.value) )
        sys.stdout.flush()


        # TODO: do not trip breaker     mgr.meter.phase_1_current


        #   Timer to Start Charge
        #
        if self.is_online and self.energy.value < self.force_charge_until_kWh.value:   # Force charge?
            self.start_counter.to_maximum() # tweak counters so charge starts immediately and does not stop
            self.stop_counter.to_maximum()
            # set lower bound for charge current to the minimum allowed by force charge
            # it is still allowed to use more power if there is more available
            self.current_limit_counter.minimum = self.force_charge_minimum_A.value
        else:
            # monitor excess power to decide if we can start charge
            # do it before checking the socket state so when we plug in, it starts immediately
            # if power is available
            self.start_counter.addsub( excess >= self.start_excess_threshold_W.value, time_since_last_call )
            self.current_limit_counter.minimum = self.i_start   # set lower bound at minimum allowed current

        # Are we connected? If RS485 fails, EVSE will timeout and stop charge.
        if not (self.is_online and self.local_meter.is_online):
            return

        #   Plug/Unplug
        #
        self._prev_car_ready, self.car_ready = self.car_ready, (self.socket_state.value == 0x111) # EV plugged and ready to charge
        if not self.car_ready:          # EV is not plugged in.
            if self._prev_car_ready:    # it was just unplugged.
                log.info("EVSE: EV unplogged. Reset settings.")
                self.force_charge_until_kWh.set( 0 ) # reset it after unplugging car
                self.stop_charge_after_kWh .set( 0 )  
            return await self.pause_charge( )

        # now, EV is plugged in, but not necessarily authorized or willing to charge.
        # charge_state 500     : waiting for RFID authorization or other causes.
        #              1-2-300 : waiting for the car or current limit below 6A, not charging
        #              400     : charging
        # and... it's not necessary to check: if it wants to charge, it will do it automatically.

        # Energy limit?
        if (v := self.stop_charge_after_kWh.value) and self.energy.value >= v:
            return await self.pause_charge( )

        # Should we start charge ?
        if self.is_charge_paused():
            if self.start_counter.at_maximum():
                await self.resume_charge()
            # return in all cases, since the car takes 30s to start up, so we have nothing special to do now
            return

        # now we're in charge mode, not paused
        power   = self.local_meter.active_power.value
        voltage = self.local_meter.voltage.value            # Use car's actual power use
        if power < self.charge_detect_threshold_W.value:
            # Current is low. This either means:
            # - Charge hasn't started yet, so we shouldn't issue new current adjustments and whack the current limit 
            # into the maximum and then charging would start with a big current spike.
            # - Or it is in the final balancing stage, and we want that to finish cleanly no matter what excess PV is.
            # In both cases, just do nothing, except prolong the timeout to delay the next current_limit update/
            self.command_interval.reset( 5 )
            self._begin_charge_timestamp = time.monotonic()
            return

        # Should we stop charge ? Do not move this check earlier as that would interrupt the final balancing stage
        if self.stop_counter.at_minimum():
            return await self.pause_charge()

        # Soft start
        self.current_limit_counter.set_maximum( max( self.i_start, min( self.i_max, time.monotonic() - self._begin_charge_timestamp )))

        # Is EVSE power reading stable?
        if max(self.local_meter.power_history) - min(self.local_meter.power_history) > self.stability_threshold_W.value:
            return  # power is changing: wait before doing adjustments.

        # Finally... adjust current.
        # Since the car imposes 1A steps, don't bother with changes smaller than this.
        if abs(excess) < self.dead_band_W.value:
            return

        # calculate new limit
        cur_limit = self.current_limit_counter.value
        new_limit = round( (power + excess) * self.control_gain.value / voltage )
        self.stop_counter.addsub( new_limit >= self.i_start, time_since_last_call ) # stop charge if we're at minimum power and repeatedly try to go lower
        new_limit = max( self.i_start, min( self.i_max, new_limit ))                # clip it so we keep charging until stop_counter says we stop
        delta_i = new_limit - cur_limit

        self.mqtt.publish_value( self.mqtt_topic+"command_interval_small",  round(self.command_interval_small.remain(),1) )
        self.mqtt.publish_value( self.mqtt_topic+"command_interval",        round(self.command_interval.remain(),1) )

        print( "power %4d voltage %4d current %5.02f limit %d -> %d timeouts %.02f %.02f" % (power, voltage, power/voltage, cur_limit, new_limit, self.command_interval.remain(), self.command_interval_small.remain() ))

        if not delta_i:
            return
        # do not constantly send small changes
        if abs( delta_i ) <= self.small_current_step_A.value and not self.command_interval_small.expired():
            return
        if not self.command_interval.expired():
            return

        # If a change is needed, it means power is changing, and that means we should wait for readings 
        # to stabilize.  This avoids sending a small change at the beginning of a step, then having to wait for
        # the car to update to proces the rest of the step.
        if not self.settle_timeout.expiry:      # Timeout is not set
            self.settle_timeout.reset( self.settle_timeout_ms.value * 0.001 )   # Set timeout, when it expires we end up at the next line

        if self.settle_timeout.expired():     # Execute current limit change
            await self.set_current_limit( new_limit )
            self.command_interval.reset( self.command_interval_ms.value * 0.001 )
            self.command_interval_small.reset( self.command_interval_small_ms.value * 0.001 )  # prevent frequent small updates

        #### OLD ALGO

    #     # Are we waiting for power to settle after a command?
    #     if self.target_power:   # this is the power we told the car to take
    #         mi, ma = self.target_power      # it's actually an [interval]
    #         # print( "target: %d<%d<%d" % (mi, power, ma))
    #         if mi <= power <= ma:
    #             # print("in range")
    #             self.target_power = None
    #             self.publish_target_power()       # publish it so the mqtt graph shows target has been reached
    #             self.command_interval.at_most(1)  # if timeout is still running, shorten it
    #             self.command_interval_small.at_most(1)  # if timeout is still running, shorten it

    #     # Finally... adjust current.
    #     # Since the car imposes 1A steps, if we have a small excess that is lower than 1A then don't bother.
    #     delta_i = 0
    #     if abs(excess) > self.p_dead_band:
    #         delta_i     = round( excess * 0.004 )

    #     # stop charge if we're at minimum power and repeatedly try to go lower
    #     if delta_i < 0 and self.current_limit_counter.at_minimum():
    #         self.stop_counter.add( -time_since_last_call )
    #     else:
    #         self.stop_counter.add( time_since_last_call )


    #     delta_i = limit - self.current_limit_counter.value # did it change?
    #     if not dry_run:
    #         await self.set_current_limit( limit )
    #     return delta_i

    #     change = await self.adjust_current_limit( delta_i, dry_run=True )

    #     # print( "excess %5d power %5d ilim %5d delta %s change %5d ssc %s" % (excess, power, self.rwr_current_limit.value, delta_i, change, self.stop_counter.value ))
    #     if not change:
    #         return

    #     self.mqtt.publish_value( self.mqtt_topic+"command_interval_small",  round(self.command_interval_small.remain(),1) )
    #     self.mqtt.publish_value( self.mqtt_topic+"command_interval",        round(self.command_interval.remain(),1) )

    #     # print("timeout remain", self.command_interval.remain())
    #     if not self.command_interval.expired():
    #         # we're not sure the previous command has been executed by the car, so wait.
    #         return

    #     # Now charge.
    #     # EVSE is slow, the car takes >2 seconds to respond to commands, then the inverter needs to adjust,
    #     # new value of excess power needs to stabilize, etc, before a new power setting can he issued.
    #     # This means we have to wait long enough between commands.

    #     # do we want to change the current setting? (this enforces min/max current bounds)
    #     change = await self.adjust_current_limit( delta_i, dry_run=True )

    #     # print( "excess %5d power %5d ilim %5d delta %s change %5d ssc %s" % (excess, power, self.rwr_current_limit.value, delta_i, change, self.stop_counter.value ))
    #     if not change:
    #         return

    #     # special case: do not bang it constantly with lots of small adjustments
    #     if abs(delta_i) <= 2 and not self.command_interval_small.expired():
    #         return

    #     # print( "execute", delta_i )
    #     self.previous_delta = delta_i = await self.adjust_current_limit( delta_i )
    #     self.command_interval_small.reset( 6 )  # wait time for next small update

    #     # set target so we know it's executed
    #     if abs(delta_i) > 2:
    #         self.command_interval.reset( 9 )        # wait time for next large update
    #         target_i = self.rwr_current_limit.value
    #         voltage     = self.local_meter.voltage.value
    #         self.target_power = power+(delta_i-1.8)*voltage, power+(delta_i+1.8)*voltage
    #         self.publish_target_power()

    # async def adjust_current_limit( self, increment, dry_run=False ):
    #     # the car rounds current to the nearest integer number of amps, so 
    #     # the counter automatically rounds too, this avoids useless writes
    #     limit = self.current_limit_counter.pretend_add( increment )
    #     delta_i = limit - self.current_limit_counter.value # did it change?
    #     if not dry_run:
    #         await self.set_current_limit( limit )
    #     return delta_i

    async def set_current_limit( self, current_limit ):
        current_limit = round(current_limit)
        self.current_limit_counter.set( current_limit )

        if self.resend_current_limit_tick.ticked():        
            # sometimes the EVSE forgets the current limit, so send it at regular intervals
            # even if it did not change
            await self.rwr_current_limit.write( current_limit )
            wrote = True
        else:
            # otherwise write only if changed
            wrote = await self.rwr_current_limit.write_if_changed( current_limit )

        if wrote:
            # print( "write limit", current_limit )
            mqtt = self.mqtt
            topic = self.mqtt_topic
            mqtt.publish_reg( topic, self.rwr_current_limit )
            if config.LOG_MODBUS_WRITE_REQUEST_TIME:
                self.publish_modbus_timings()

