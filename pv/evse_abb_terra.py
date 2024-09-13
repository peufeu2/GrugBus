#!/usr/bin/python
# -*- coding: utf-8 -*-

import time, asyncio, datetime, logging, collections, traceback, pymodbus
from pymodbus.exceptions import ModbusException
from asyncio.exceptions import TimeoutError

# Device wrappers and misc local libraries
import grugbus
from grugbus.devices import EVSE_ABB_Terra
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

        self.rwr_current_limit.value = 0.0

        self.regs_to_read = (
            self.charge_state       ,
            self.current_limit      ,
            self.current            ,
            # self.active_power       ,
            self.energy             ,
            self.error_code         ,
            self.socket_state
        )

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
        self.command_interval       = Timeout( 10, expired=True )
        self.command_interval_small = Timeout( 10, expired=True )
        self.resend_current_limit_timeout = Timeout( 10, expired=True )

        # settings, DO NOT CHANGE as these are set in the ISO standard
        self.i_pause = 5.0          # current limit in pause mode
        self.i_start = 6.0          # minimum current limit for charge, can be set to below 6A if charge should pause when solar not available
        self.i_max   = 30.0         # maximum current limit

        # see comments in set_virtual_current_limit()
        self.virtual_i_min   = -5.0   # bounds
        self.virtual_i_max   = 32.0  # bounds
        self.virtual_i_pause   = -3.0 # hysteresis comparator: virtual_current_limit threshold to go from charge to pause
        self.virtual_i_unpause = 1.0 # hysteresis comparator: virtual_current_limit threshold to go from pause to charge
        self.virtual_current_limit = self.virtual_i_min

        self.ensure_i   = 6         # guarantee this charge current
        self.ensure_Wh  = 0       # until this energy has been delivered
        self.p_threshold_start = 1400   # minimum excess power to start charging

        self.p_dead_band_fast = 0.5 * 240
        self.p_dead_band_slow = 0.5 * 240

        self.tick      = Metronome( config.POLL_PERIOD_EVSE )   # how often we poll it over modbus
        self.event_all = asyncio.Event()  # Fires when all registers are read, for slower processes

        self.route_tick = Metronome( 1 )
        self.avg_excess = MovingAverage( 10 )
        self.target_power   = None
        self.previous_delta = 0
        self.settled        = False

    async def stop( self ):
        await self.set_virtual_current_limit( self.virtual_i_min )

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

    async def route( self, excess ):
        # Moving average of excess power (returns None is not enough data was accumulated yet)
        avg_excess  = self.avg_excess.append( excess )
        power       = self.local_meter.active_power.value
        voltage     = self.local_meter.voltage.value

        # if EV is not plugged, or everything is not initialized yet, do not charge
        if ((self.socket_state.value) != 0x111 or None in (power, voltage, avg_excess) ):
            # set charge to paused, it will take many loops to increment this enough to start
            # print("noinit", self.socket_state.value, power, voltage, avg_excess)
            await self.set_virtual_current_limit( self.virtual_i_min )
            return

        # TODO: do not trip breaker     mgr.meter.phase_1_current

        # now, EV is plugged in, but not necessarily authorized or willing to charge.
        # charge_state 500     : waiting for RFID authorization or other causes.
        #              1-2-300 : waiting for the car or current limit below 6A, not charging
        #              400     : charging

        # At the beginning of charge, deliver requested amount of power no matter what
        if self.energy.value < self.ensure_Wh:
            await self.set_virtual_current_limit( self.ensure_i )    # set current to minimum value to allow charging to begin
            print("force charge")
            return

        #
        #   TODO: charging start/stop logic is defective, it starts too fast and doesn't stop unless there is way too much power draw
        #

        # Should we start charge ?
        if self.is_charging_paused():
            if avg_excess > self.p_threshold_start:
                await self.set_virtual_current_limit( self.i_start )
                print("start")
            return

        # now we're in charge mode
        if power < 1265:    
            # Current is low. This either means:
            # - Charge hasn't started yet, so we shouldn't issue new current adjustments that will be ignored... as the result of that
            # would be incrementing the current limit too much and when charging starts, a big current spike.
            # - Or it is in the final balancing stage, and we want that to finish cleanly no matter what excess PV is.
            # In both cases, we just do nothing.
            self.command_interval.reset( 6 )
            self.target_power = None
            print("startup")
            return

        # Finally... adjust current.
        # Since the car imposes 1A steps, if we have a small excess that is lower than the power step
        # then don't bother.
        delta_i = 0
        if abs(excess) > self.p_dead_band_fast:
            delta_i     = excess * 0.004

        # Now charge with solar only
        # EVSE is slow, the car takes 2 seconds to respond to commands, then the inverter needs to adjust,
        # new value of excess power needs to stabilize, etc, before a new power setting can he issued.
        # This means we have to wait long enough between commands.
        change = await self.adjust_virtual_current_limit( delta_i, dry_run=True )
        if not change:
            # Next time, if a change is required, we will wait a little
            # this is to wait for power to settle
            self.command_interval.at_least(1)
            return

        print( "excess %5d power %5d ilim %5d change %5d" % (excess, power, self.rwr_current_limit.value, change))

        # do not bang it with lots of small adjustments
        if abs(delta_i) <= 2 and not self.command_interval_small.expired():
            return

        print("timeout remain", self.command_interval.remain())
        if not self.command_interval.expired():
            # For a large adjustment we can override the timout
            # by checking if the car is drawing the power we requested
            if self.target_power:
                # try to shorten timeout
                mi, ma = self.target_power
                print( "target: %d<%d<%d" % (mi, power, ma))
                if mi <= power <= ma:
                    print("in range")
                    self.target_power = None
                    # self.command_interval.at_most(5)        # previous  command executed: shorten timeout
                    self.command_interval.at_most(1)        # previous  command executed: shorten timeout
            return

        print( "execute", delta_i )
        await self.adjust_virtual_current_limit( delta_i )
        self.previous_delta = delta_i
        self.command_interval.reset( 9 )
        self.command_interval_small.reset( 6 )

        # set target so we know it's executed
        target_i = self.rwr_current_limit.value
        self.target_power = power+(delta_i-1.8)*voltage, power+(delta_i+1.8)*voltage
        self.settled = False
        self.mqtt.publish_value( self.mqtt_topic+"target_power_min",  round(self.target_power[0],2) )
        self.mqtt.publish_value( self.mqtt_topic+"target_power_max",  round(self.target_power[1],2) )


    def is_charging_paused( self ):
        return self.rwr_current_limit.value < self.i_start

    def is_charging_unpaused( self ):
        return self.rwr_current_limit.value >= self.i_start

    async def adjust_virtual_current_limit( self, increment, dry_run=False ):
        if increment:
            # make it slow to shut down but fast to start again when a cloud passes
            newval = self.virtual_current_limit + increment
            # if newval < self.virtual_i_unpause:
            #     increment = max( -0.1, increment )
            #     newval = self.virtual_current_limit + increment
            return await self.set_virtual_current_limit( newval, dry_run )
        else:
            return 0

    # Adjusting current when charging takes a few seconds so we can do it often.
    # But pausing and restarting charge is slow (>30s) so we should not do it just because there was a spike in the
    # exported power measured by the meter.
    # When current limit is 6A, EVSE will start charging. Below that it will pause.
    # So we use a virtual_current_limit that is adjusted according to excess power, then derive the real current limit from it:
    # when the virtual current limit is lower than 6A but not too low, we keep the real current limit at 6A so it keeps charging.
    # This avoids short pause/restart cycles;
    async def set_virtual_current_limit( self, v_limit, dry_run=False ):
        # clip it
        v_limit = min( self.virtual_i_max, max( self.virtual_i_min, v_limit ))

        # hysteresis comparator
        threshold = self.virtual_i_pause if self.is_charging_unpaused() else self.virtual_i_unpause

        if v_limit <= threshold:      current_limit = self.i_pause     # very low: pause charging
        elif v_limit <= self.i_start: current_limit = self.i_start     # low but not too low: keep charging at min power
        else:                         current_limit = min( self.i_max, v_limit ) # above 6A: send current limit unchanged

        # avoid useless writes since the car rounds it
        current_limit = round(current_limit)

        # returns True if the value changed
        delta_i = current_limit - self.rwr_current_limit.value

        # execute if dry_run is false
        if not dry_run:
            self.virtual_current_limit = v_limit
            if delta_i or self.resend_current_limit_timeout.expired():
                self.resend_current_limit_timeout.reset()
                await self.rwr_current_limit.write( current_limit )
                print( "write limit", current_limit )

                # set meter poll period depending on state
                self.local_meter.tick.set( config.POLL_PERIOD_EVSE_METER[0] * ( 10 if self.is_charging_paused() else 1 ))

                mqtt = self.mqtt
                topic = self.mqtt_topic
                mqtt.publish_reg( topic, self.rwr_current_limit )
                mqtt.publish_value( topic+"virtual_current_limit",  round(self.virtual_current_limit,2) )
                mqtt.publish_value( topic+"charging_unpaused"    ,  self.is_charging_unpaused() )
                if config.LOG_MODBUS_WRITE_REQUEST_TIME:
                    self.mqtt.publish_value( self.mqtt_topic+"req_time", round( self.last_transaction_duration,2 ) )
        return delta_i

