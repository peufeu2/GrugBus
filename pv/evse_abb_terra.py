#!/usr/bin/python
# -*- coding: utf-8 -*-

import time, asyncio, logging, collections, sys
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
        MQTTSetting( self, "start_excess_threshold_W"   , int  , range( 0, 2001 )            , 1400     , self.setting_updated )  # minimum excess power to start charging, unless overriden by:    
        MQTTSetting( self, "charge_detect_threshold_W"  , int  , range( 0, 2001 )            , 1200     , self.setting_updated )  # minimum excess power to start charging, unless overriden by:    

        MQTTSetting( self, "force_charge_minimum_A"     , int  , range( 6, 32 )              , 10       , self.setting_updated )  # guarantee this charge minimum current    
        MQTTSetting( self, "force_charge_until_kWh"     , int  , range( 0, 81 )              , 0        , self.setting_updated )  # until this energy has been delivered (0 to disable force charge)
        MQTTSetting( self, "stop_charge_after_kWh"      , int  , range( 0, 81 )              , 6        , self.setting_updated )

        MQTTSetting( self, "offset"                     , int  , range( -10000, 10000 )      , 0        , self.setting_updated )

        MQTTSetting( self, "settle_timeout_s"           , float, (lambda x: 0.1<=x<=10)     , 1.0   , self.setting_updated )
        MQTTSetting( self, "command_interval_s"         , float, (lambda x: 0.1<=x<=10)     , 0.5   , self.setting_updated )
        MQTTSetting( self, "command_interval_small_s"   , float, (lambda x: 1<=x<=10)       , 9     , self.setting_updated )
        MQTTSetting( self, "up_timeout_s"               , float, (lambda x: 0.1<=x<=20)     , 15    , self.setting_updated )
        MQTTSetting( self, "end_of_charge_timeout_s"    , float, (lambda x: 0.1<=x<=320)     , 120    , self.setting_updated )

        MQTTSetting( self, "dead_band_W"                , int  , range( 0, 501 )             ,  0.5*240 , self.setting_updated )        # dead band for power routing
        MQTTSetting( self, "stability_threshold_W"      , int  , range( 50, 201 )            ,  150     , self.setting_updated )
        MQTTSetting( self, "small_current_step_A"       , int  , ( 1, 2 )                    ,  1       , self.setting_updated )
        MQTTSetting( self, "control_gain_p"             , float, (lambda x: 0.1 <= x <= 0.99),  0.96    , self.setting_updated )
        MQTTSetting( self, "control_gain_i"             , float, (lambda x: 0 <= x <= 0.1)   ,  0.05    , self.setting_updated )

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
        self.stop_counter  = BoundedCounter( 0, -60, 60 )  # charge stops  when this counter reaches minimum
        self.integrator    = BoundedCounter( 0, -160, 160 )    # in Watts

        # current limit that will be sent to the EVSE, incremented and decremented depending on available power
        self.current_limit_bounds = BoundedCounter( self.i_start, self.i_start, self.i_max, round )

        #   After each command:
        #       The car's charger takes 2-3s to react, but we don't know yet if the charger will decide to use all of the allowed power, or only some.
        #       Then the solar inverter adjusts its output power, which takes another 1-2s. 
        #   During all the above, excess PV power calculated by the Router class includes power the car is about to take, 
        #   so it not be used to trigger some other loads which would result in "overbooking" of PV power.
        #   In addition, excess PV power needs to be smoothed (see Router class) to avoid freaking out every time a motor is started in the house.
        #   Excess power also needs to settle before it can be used to calculate a new current_limit command.
        #   All this means it will be quite slow.
        self.command_interval          = Timeout( self.command_interval_s         .value, expired=True ) # different intervals depending on size of current_limit adjustment (see below)
        self.settle_timeout            = Timeout( self.settle_timeout_s           .value, expired=True ) # different intervals depending on size of current_limit adjustment (see below)
        self.command_interval_small    = Timeout( self.command_interval_small_s   .value, expired=True ) # different intervals depending on size of current_limit adjustment (see below)
        self.up_timeout                = Timeout( self.up_timeout_s               .value, expired=True )      # after reducing current, wait before increasing it again

        self.resend_current_limit_tick = Metronome( 10 )    # sometimes the EVSE forgets the setting, got to send it once in a while
        self.unplug_timeout            = Timeout( 3 )

        self.end_of_charge_timeout     = Timeout( self.end_of_charge_timeout_s.value )

        self.busy_timeout = Timeout( 8 )

        self._last_call     = Chrono()
        self.car_ready = None    # if car is plugged in and ready
        self.soft_start_timestamp = 0                # set when car begins to draw current, for soft start

        # State of this softwate (not the charger)
        self.set_state( self.STATE_UNPLUGGED )

    STATE_UNPLUGGED = 0
    STATE_PLUGGED   = 1
    STATE_CHARGING  = 2
    STATE_FINISHING = 3
    STATE_FINISHED  = 4

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
            self.set_state( min( self.state, STATE_CHARGING ))

    # Async stuff that can't be in constructor
    async def initialize( self ):
        await self.setting_updated()
        await self.pause_charge()

    def is_charge_paused( self ):
        return self.rwr_current_limit.value < self.i_start

    def is_charge_unpaused( self ):
        return self.rwr_current_limit.value >= self.i_start

    async def pause_charge( self, print_log=True ):
        if self.is_charge_unpaused():
            log.info("EVSE: Pause charge")
        self.local_meter.tick.set( config.POLL_PERIOD_EVSE_METER* 10 )   # less MQTT traffic when not charging
        self.start_counter.to_minimum() # reset counters 
        self.stop_counter.to_minimum()
        await self.set_current_limit( self.i_pause )

    async def resume_charge( self ):
        if self.is_charge_paused():
            log.info("EVSE: Resume charge")
            self.end_of_charge_timeout.reset( self.end_of_charge_timeout_s.value )
        self.local_meter.tick.set( config.POLL_PERIOD_EVSE_METER ) # poll meter more often
        self.start_counter.to_maximum()
        self.stop_counter.to_maximum()
        self.integrator.set(0)
        await self.set_current_limit( self.i_start )

    def advance_state( self, state ):
        self.set_state( max( self.state, state ) )

    def set_state( self, state ):
        self.state = state
        self.mqtt.publish_value( self.mqtt_topic+"state",  state )

    async def read_coroutine( self ):
        mqtt = self.mqtt
        topic = self.mqtt_topic
        while True:
            try:
                await self.tick.wait()
                await self.read_regs( self.regs_to_read )
                for reg in self.regs_to_read:
                    mqtt.publish_reg( topic, reg )

                # publish force charge info                
                if config.LOG_MODBUS_REQUEST_TIME_ABB:
                    self.publish_modbus_timings()

            except (TimeoutError, ModbusException):
                await asyncio.sleep(1)

            except Exception:
                log.exception(self.key+":")
                await asyncio.sleep(1)

            self.event_all.set()
            self.event_all.clear()

    #   Main power routing func
    #
    async def route( self, excess ):
        time_since_last_call = min( 1, self._last_call.lap() )

        excess += self.offset.value
        # sys.stdout.write( "\revse: exc %-4d start %-2d stop %-2d power %d " % (excess, self.start_counter.value, self.stop_counter.value, self.local_meter.active_power.value) )
        # sys.stdout.flush()


        # TODO: do not trip breaker     mgr.meter.phase_1_current

        #   Timer to Start Charge
        #
        if self.is_online and self.energy.value < self.force_charge_until_kWh.value:   # Force charge?
            self.start_counter.to_maximum() # tweak counters so charge starts immediately and does not stop
            self.stop_counter.to_maximum()
            # set lower bound for charge current to the minimum allowed by force charge
            # it is still allowed to use more power if there is more available
            self.current_limit_bounds.set_minimum( self.force_charge_minimum_A.value )
        else:
            # monitor excess power to decide if we can start charge
            # do it before checking the socket state so when we plug in, it starts immediately
            # if power is available
            self.start_counter.addsub( excess >= self.start_excess_threshold_W.value, time_since_last_call )
            self.current_limit_bounds.set_minimum( self.i_start )   # set lower bound at minimum allowed current

        # Are we connected? If RS485 fails, EVSE will timeout and stop charge.
        if not (self.is_online and self.local_meter.is_online):
            return

        #   Plug/Unplug
        #
        self.car_ready = (self.socket_state.value == 0x111)  # EV plugged and ready to charge
        if not self.car_ready:          # EV is not plugged in.
            if self.unplug_timeout.expired_once():
                log.info("EVSE: EV unplugged. Reset settings.")
                self.force_charge_until_kWh.set( 0 ) # reset it after unplugging car
                self.stop_charge_after_kWh .set( 0 )
                self.set_state( self.STATE_UNPLUGGED )
                await self.pause_charge( )
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
        if (v := self.stop_charge_after_kWh.value) and self.energy.value >= v:
            self.advance_state( self.STATE_FINISHED )
            return await self.pause_charge( )

        # Should we start charge ?
        if self.is_charge_paused():
            if self.start_counter.at_maximum():
                await self.resume_charge()
            # return in all cases, since the car takes 30s to start up, so we have nothing special to do now
            return

        # now we're in charge mode, not paused
        self.advance_state( self.STATE_CHARGING )
        power   = self.local_meter.active_power.value
        voltage = self.local_meter.voltage.value            # Use car's actual power use
        if power < self.charge_detect_threshold_W.value:
            # Current is low. This either means:
            # - Charge hasn't started yet, so we shouldn't issue new current adjustments and whack the current limit 
            # into the maximum and then charging would start with a big current spike.
            # - Or it is in the final balancing stage, and we want that to finish cleanly no matter what excess PV is.
            # In both cases, just do nothing, except prolong the timeout to delay the next current_limit update
            self.command_interval.reset( 5 )
            self.soft_start_timestamp = time.monotonic()
            if self.end_of_charge_timeout.expired():
                # Low current for a long time means the charge is finishing
                # Note unless we specify a maximum energy, it is not possible to know
                # if charge is finished. The car sometimes wakes up to draw a bit of power.
                self.advance_state( self.STATE_FINISHING )
        else:
            self.end_of_charge_timeout.reset( self.end_of_charge_timeout_s.value )

        # Should we stop charge ? Do not move this check earlier as that would interrupt the final balancing stage
        if self.stop_counter.at_minimum():
            return await self.pause_charge()

        # Soft start
        self.current_limit_bounds.set_maximum( max( self.i_start, min( self.i_max, time.monotonic() - self.soft_start_timestamp )))

        # Is EVSE power reading stable?
        if max(self.local_meter.power_history) - min(self.local_meter.power_history) > self.stability_threshold_W.value:
            return  # power is changing: wait before doing adjustments.

        # Finally... adjust current.
        # Since the car imposes 1A steps, don't bother with changes smaller than this.
        if abs(excess) < self.dead_band_W.value:
            self.integrator.value = 0.
            return

        # calculate new limit
        cur_limit = self.rwr_current_limit.value
        self.integrator.add( excess * self.control_gain_i.value * time_since_last_call )
        new_limit = (power + excess + self.integrator.value) * self.control_gain_p.value / voltage
        self.stop_counter.addsub( new_limit >= self.i_start, time_since_last_call ) # stop charge if we're at minimum power and repeatedly try to go lower
        new_limit = self.current_limit_bounds.clip( new_limit )                # clip it so we keep charging until stop_counter says we stop
        delta_i = new_limit - cur_limit

        # self.mqtt.publish_value( self.mqtt_topic+"command_interval_small",  round(self.command_interval_small.remain(),1) )
        # self.mqtt.publish_value( self.mqtt_topic+"command_interval",        round(self.command_interval.remain(),1) )

        print( "power %4d voltage %4d current %5.02f integ %.02f limit %d -> %d timeouts %.02f %.02f" % (power, voltage, power/voltage, self.integrator.value, cur_limit, new_limit, self.command_interval.remain(), self.command_interval_small.remain() ))

        if not delta_i:
            return

        # do not constantly send small changes
        if abs( delta_i ) <= self.small_current_step_A.value:
            if not self.command_interval_small.expired():
                return
        else:
            self.integrator.value = 0.

        # Signal to router: we're busy allowating power
        self.busy_timeout.reset()

        if not self.command_interval.expired():
            return

        # If a change is needed, it means power is changing, and that means we should wait for readings 
        # to stabilize.  This avoids sending a small change at the beginning of a step, then having to wait for
        # the car to update to proces the rest of the step.
        if not self.settle_timeout.expiry:      # Timeout is not set
            self.settle_timeout.reset( self.settle_timeout_s.value )   # Set timeout, when it expires we end up at the next line

        if not self.settle_timeout.expired():
            return

        # after reducing current, wait until bringing it back up
        if delta_i>0 and not self.up_timeout.expired():
            return
        else:
            # we're reducing current
            self.up_timeout.reset( self.up_timeout_s.value )

        # Execute current limit change
        await self.set_current_limit( new_limit )
        self.command_interval.reset( self.command_interval_s.value )
        self.command_interval_small.reset( self.command_interval_small_s.value )  # prevent frequent small updates

    # Used by router.
    # False signals the router can use excess power to do its thing.
    # True means either we have some commands in flight, or we're not at max power and would like to
    # def is_busy( self ):
    #     if self.state >= self.STATE_FINISHING: return True
    #     if self.state >= self.STATE_FINISHING: return True
    #     if self.busy_timeout.expired(): return False
    #     and self.rwr_current_limit.value 


    async def set_current_limit( self, current_limit ):
        current_limit = round(current_limit)
        # print("set limit", current_limit, "minmax", self.current_limit_bounds.minimum, self.current_limit_bounds.maximum, "value", self.current_limit_bounds.value, "reg", self.rwr_current_limit.value )

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

