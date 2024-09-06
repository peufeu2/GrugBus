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
            self.active_power       ,
            self.energy             ,
            self.error_code         ,
            self.socket_state
        )

        self.tick_poll = Metronome( config.POLL_PERIOD_EVSE )   # how often we poll it over modbus

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
        self.command_interval_large = Timeout( 10, expired=True )
        self.settle_timeout         = Timeout( 1, expired=True )
        self.settle_timeout_wait    = False
        self.resend_current_limit_timeout = Timeout( 10, expired=True )

        # settings, DO NOT CHANGE as these are set in the ISO standard
        self.i_pause = 5.0          # current limit in pause mode
        self.i_start = 6.0          # minimum current limit for charge, can be set to below 6A if charge should pause when solar not available
        self.i_max   = 30.0         # maximum current limit

        # see comments in set_virtual_current_limit()
        self.virtual_current_limit = 0
        self.virtual_i_min   = -5.0   # bounds
        self.virtual_i_max   = 32.0  # bounds
        self.virtual_i_pause   = -3.0 # hysteresis comparator: virtual_current_limit threshold to go from charge to pause
        self.virtual_i_unpause = 1.0 # hysteresis comparator: virtual_current_limit threshold to go from pause to charge

        self.ensure_i   = 6         # guarantee this charge current
        self.ensure_Wh  = 0       # until this energy has been delivered
        self.p_threshold_start = 1400   # minimum excess power to start charging

        self.p_dead_band_fast = 0.5 * 240
        self.p_dead_band_slow = 0.5 * 240

    def publish( self, all=True ):
        if all: 
            pub = { reg.key: reg.format_value() for reg in self.regs_to_read }
            if config.LOG_MODBUS_REQUEST_TIME:
                pub["req_time"] = round( self.last_transaction_duration,2 )
        else:
           pub = {}
        pub["virtual_current_limit"] = "%.02f"%self.virtual_current_limit
        pub["rwr_current_limit"]     = self.rwr_current_limit.format_value()
        pub["charging_unpaused"]     = self.is_charging_unpaused()
        self.mqtt.publish( self.mqtt_topic, pub )

    async def poll( self, p, fast ):
        try:
            await self.connect()
            await self.read_regs( self.regs_to_read )
            self.publish()
            # line = "%d %-6dW e=%6d s=%04x s=%04x v%6.03fA %6.03fA %6.03fA %6.03fA %6.03fW %6.03fWh %fs" % (fast, p, self.error_code.value, self.charge_state.value, self.socket_state.value, 
            #     self.virtual_current_limit, self.rwr_current_limit.value, self.current_limit.value, self.current.value, 
            #     self.active_power.value, self.energy.value, self.last_transaction_duration )
            # print( line )
            return True
        except Exception:
            log.exception(self.key+":")
            # await asyncio.sleep(0.5)

    async def stop( self ):
        await self.set_virtual_current_limit( self.virtual_i_min )

    async def route( self, excess, fast_route ):
        # Poll charging station
        if self.tick_poll.ticked():         # polling interval

            # poll EVSE over modbus
            if not await self.poll( excess, fast_route ):
                self.tick_poll.tick = 30.0  # If it did not respond, poll it less often to not disturb the shared smartmeter modbus line
                self.default_retries = 1
                return
            self.tick_poll.tick = 1.0   # EVSE is online, use short poll interval
            self.default_retries = 2

        # EV is not plugged, or init register at startup
        if (self.socket_state.value) != 0x111 or (not self.rwr_current_limit.value):    
            # set charge to paused, it will take many loops to increment this enough to start
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
            return

        if self.is_charging_paused():
            # Increment it slowly, so we need to see enough excess for long enough to start a charge
            await self.adjust_virtual_current_limit( 0.5 if excess > self.p_threshold_start else -0.5 )
            return

        if self.current.value < 5.5:    
            # Current is low. This either means:
            # - Charge hasn't started yet, so we shouldn't issue new current adjustments that will be ignored... as the result of that
            # would be incrementing the current limit too much and when charging starts, a big current spike.
            # - Or it is in the final charging/balancing stage, and we want that to finish cleanly no matter what excess PV is.
            # In both cases, we just do nothing.
            self.command_interval.reset( 6 )
            self.command_interval_large.reset( 6 )
            return

        # Finally... adjust current.
        # Since the car imposes 1A steps, if we have a small excess that is lower than the power step
        # then don't bother.
        wait_time = 0
        delta_i = 0
        if fast_route:
            if abs(excess) > self.p_dead_band_fast:
                delta_i     = excess * 0.004
                wait_time = 5   # SAE J1772 specs: max charger response time to PWM change is 5s
        else:
            if abs(excess) > self.p_dead_band_slow:
                delta_i     = excess * 0.004
                wait_time = 9  # add some wait time due to slow inverter reaction

        # Now charge with solar only
        # EVSE is slow, the car takes 2 seconds to respond to commands, then the inverter needs to adjust,
        # new value of excess power needs to stabilize, etc, before a new power setting can he issued.
        # This means we have to wait long enough between commands.
        change = await self.adjust_virtual_current_limit( delta_i, dry_run=True )
        # print( "delta_i", delta_i, "change", change, "")
        if abs(change) <= 1:                   # small change in current
            if self.command_interval.expired():
                await self.adjust_virtual_current_limit( delta_i )
                self.command_interval.reset( wait_time )
                self.command_interval_large.reset( 2 )      # allow a large change quickly after a small one    
        else:     # we need a quick large change
            if self.command_interval_large.expired(): 
                # ready to issue command
                # todo: fine tune this, if the inverters receives the updated meter readings
                # at the same time it may do the same thing as the router...
                if not self.settle_timeout_wait:
                    # large step: wait one extra second for it to settle
                    self.settle_timeout_wait = True
                    self.settle_timeout.reset(0.9)
                else:
                    if self.settle_timeout.expired():
                        await self.adjust_virtual_current_limit( delta_i )
                        self.command_interval.reset( wait_time )
                        self.command_interval_large.reset( wait_time )
                        self.settle_timeout_wait = False

        # inform main router that we have requested a power change to the car
        # so it should avoid routing other slow loads during this delay
        return wait_time    


    def is_charging_paused( self ):
        return self.rwr_current_limit.value < self.i_start

    def is_charging_unpaused( self ):
        return self.rwr_current_limit.value >= self.i_start

    async def adjust_virtual_current_limit( self, increment, dry_run=False ):
        if increment:
            return await self.set_virtual_current_limit( self.virtual_current_limit + increment, dry_run )
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
        if not dry_run:
            self.virtual_current_limit = v_limit
            if delta_i or self.resend_current_limit_timeout.expired():
                self.resend_current_limit_timeout.reset()
                self.rwr_current_limit.value = current_limit
                self.publish( False )   # publish before writing to measure total reaction time (modbus+EVSE+car charger)
                await self.rwr_current_limit.write( )
                if config.LOG_MODBUS_WRITE_REQUEST_TIME:
                    self.mqtt.publish( self.mqtt_topic, { "req_time": round( self.last_transaction_duration,2 ) } )
        return delta_i

