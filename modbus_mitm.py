#!/usr/bin/python
# -*- coding: utf-8 -*-

import os, pprint, time, sys, serial, socket, traceback, struct, datetime, logging, math, traceback, shutil, collections
from path import Path

# This program is supposed to run on a potato (Allwinner H3 SoC) and uses async/await,
# so import the fast async library uvloop
import uvloop, asyncio, signal, aiohttp

# Modbus
import pymodbus
from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient

# Device wrappers and misc local libraries
import config
from misc import *
from pv.mqtt_wrapper import MQTTWrapper
import grugbus
from grugbus.devices import Eastron_SDM120, Eastron_SDM630, Acrel_1_Phase, EVSE_ABB_Terra, Acrel_ACR10RD16TE4
from pv.fake_meter import FakeSmartmeter
import pv.solis_s5_eh1p, pv.inverter_local_meter, pv.main_meter

"""
    python3.11
    pymodbus 3.1

TODO: move init after event loop is created
https://github.com/pymodbus-dev/pymodbus/issues/2102


#### WARNING ####
This file is both the example code and the manual.
==================================================

RS485 ports
- Master:       Solis inverters COM port + Meter attached to inverter
- Slave:        Inverter 1 fake meter
- Slave:        Inverter 2 fake meter
- Master:       Main smartmeter (gets its own interface cause it has to be fast, for power routing)

MQTT topics:
pv/                 Everything photovoltaic
pv/solis1/          Inverter 1
pv/solis1/meter     Meter attached to inverter1 (because its internal grid port power measurement sucks too much)
pv/meter/           Meter for the whole house

Reminder:
git remote set-url origin https://<token>@github.com/peufeu2/GrugBus.git

How to prevent USB serial ports from changing names: use /dev/serial/by-id/ names, not /dev/ttywhoknows

#####   Sign of power
When power can flow one way, always positive (example: PV power)
When Power can flow both ways, positive sign is always consumed power
  If something_power is positive, that something is drawing power or current
  If something_power is negative, that something is producing power
This is sometimes not intuitive, but at least it's the same convention everywhere!
    Inverter grid port power 
        positive if it is consuming power (to charge batteries for example)
        negative means it's producing
    Battery power is positive if it is charging
    Backup port power is power consumed by backup loads, always positive
    Grid side meter is positive if the house is consuming power
    etc
"""

# pymodbus.pymodbus_apply_logging_config( logging.DEBUG )
logging.basicConfig( encoding='utf-8', 
                     level=logging.INFO,
                     format='[%(asctime)s] %(levelname)s:%(message)s',
                     handlers=[logging.FileHandler(filename=Path(__file__).stem+'.log'), 
                            logging.StreamHandler(stream=sys.stdout)])
log = logging.getLogger(__name__)

# set max reconnect wait time for Fronius
# pymodbus.constants.Defaults.ReconnectDelayMax = 60000   # in milliseconds

########################################################################################
#   MQTT
########################################################################################
class MQTT( MQTTWrapper ):
    def __init__( self ):
        super().__init__( "pv" )

    # def on_connect(self, client, flags, rc, properties):
    #     self.mqtt.subscribe('cmd/pv/#', qos=0)

mqtt = MQTT()




########################################################################################
#
#       ABB Terra EVSE, controlled via Modbus
#
#       See grugbus/devices/EVSE_ABB_Terra.txt for important notes!
#
########################################################################################
class EVSE( grugbus.SlaveDevice ):
    def __init__( self, modbus ):
        super().__init__(
            modbus,                # Initialize grugbus.SlaveDevice
            3,                     # Modbus address
            "evse",                # Name (for logging etc)
            "ABB Terra",    # Pretty name 
            # List of registers is in another file, which is auto-generated from spreadsheet
            # so import it now
            EVSE_ABB_Terra.MakeRegisters() )

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
            pub["req_time"] = round( self.last_transaction_duration,2 )
        else:
           pub = {}
        pub["virtual_current_limit"] = "%.02f"%self.virtual_current_limit
        pub["rwr_current_limit"]     = self.rwr_current_limit.format_value()
        pub["charging_unpaused"]     = self.is_charging_unpaused()
        mqtt.publish( "pv/evse/", pub )

    async def poll( self, p, fast ):
        try:
            await self.read_regs( self.regs_to_read )
            self.publish()
            # line = "%d %-6dW e=%6d s=%04x s=%04x v%6.03fA %6.03fA %6.03fA %6.03fA %6.03fW %6.03fWh %fs" % (fast, p, self.error_code.value, self.charge_state.value, self.socket_state.value, 
            #     self.virtual_current_limit, self.rwr_current_limit.value, self.current_limit.value, self.current.value, 
            #     self.active_power.value, self.energy.value, self.last_transaction_duration )
            # print( line )
            return True
        except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
            raise
        except:
            s = traceback.format_exc()
            log.error(s)
            mqtt.mqtt.publish( "pv/exception", s )
            await asyncio.sleep(0.5)

    async def stop( self ):
        await self.set_virtual_current_limit( self.virtual_i_min )

    async def route( self, excess, fast_route ):
        # Poll charging station
        if not self.tick_poll.ticked():         # polling interval
            return

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

        # TODO: if a large current step comes after a small adjustment, ignore the timeout
        # basically add a load turn on/off detector

        # Finally... adjust current.
        # Since the car imposes 1A steps, if we have a small excess that is lower than the power step
        # then don't bother.
        wait_time = 0
        delta = 0
        if fast_route:
            if abs(excess) > self.p_dead_band_fast:
                delta     = excess * 0.004
                wait_time = 5   # SAE J1772 specs: max charger response time to PWM change is 5s
        else:
            if abs(excess) > self.p_dead_band_slow:
                delta     = excess * 0.004
                wait_time = 9  # add some wait time due to slow inverter reaction

        # Now charge with solar only
        # EVSE is slow, the car takes 2 seconds to respond to commands, then the inverter needs to adjust,
        # new value of excess power needs to stabilize, etc, before a new power setting can he issued.
        # This means we have to wait long enough between commands.
        change = await self.adjust_virtual_current_limit( delta, dry_run=True )
        # print( "delta", delta, "change", change, "")
        if abs(change) <= 1:                   # small change in current
            if self.command_interval.expired():
                await self.adjust_virtual_current_limit( delta )
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
                        await self.adjust_virtual_current_limit( delta )
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
        delta = current_limit - self.rwr_current_limit.value
        if not dry_run:
            self.virtual_current_limit = v_limit
            if delta or self.resend_current_limit_timeout.expired():
                self.resend_current_limit_timeout.reset()
                self.rwr_current_limit.value = current_limit
                self.publish( False )   # publish before writing to measure total reaction time (modbus+EVSE+car charger)
                await self.rwr_current_limit.write( )
                mqtt.publish( "pv/evse/", { "req_time": round( self.last_transaction_duration,2 ) } )
        return delta



class Routable():
    def __init__( self, name, power ):
        self.name = name
        self.power = power
        self.is_on = None
        self.off()

class RoutableTasmota( Routable ):
    def __init__( self, name, power, mqtt_prefix ):
        self.mqtt_prefix = mqtt_prefix
        super().__init__( name, power )

    def mqtt_power( self, message ):
        return mqtt.publish( "cmnd/%s/"%self.mqtt_prefix, {"Power": message} )

    def off( self ):
        if self.is_on:
            log.info( "Route OFF %s %s", self.name, self.mqtt_prefix )
        if self.mqtt_power( "0" ):
            self.is_on = 0

    def on( self ):
        if not self.is_on:
            log.info( "Route ON  %s %s", self.name, self.mqtt_prefix )
        self.off_counter = 0
        if self.mqtt_power( "1" ):
            self.is_on = 1

    def set( self, value ):
        if value:   self.on()
        else:       self.off()

class Router():
    def __init__( self ):
        self.devices = [ 
                         RoutableTasmota("Tasmota T4 Sèche serviette", 1050, "plugs/tasmota_t4"),
                         RoutableTasmota("Tasmota T2 Radiateur PF", 800, "plugs/tasmota_t2"),
                         RoutableTasmota("Tasmota T1 Radiateur bureau", 1700, "plugs/tasmota_t1"),
                        ]       
        self.tick_mqtt = Metronome( config.ROUTER_PUBLISH_PERIOD )

        # queues to smooth measured power, see comment on command_interval in EVSE class
        # "nobat" is for "excess power" and "bat" for "same including power we can steal from battery charging"
        self.smooth_length_slow = 35
        self.smooth_length_fast = 5
        self.smooth_export = [0] * self.smooth_length_slow
        self.smooth_bp     = [0] * self.smooth_length_slow

        # Battery charging current is limited to the product of:
        #   1) Full charging current (as requested by BMS depending on SOC)
        #   2) Scale factor below, from 0 to 1
        #   Note: when inverter is hot or soc>90%, battery charging is reduced to 85A and sometimes 
        #   this does not show up in battery_max_charge_current, so adjust below parameters to avoid
        #   wasting power
        self.battery_min_soc = 70               # Do not steal power from battery charging if SOC is below this
        self.battery_min_soc_scale = 0.98
        self.battery_max_soc = 80
        self.battery_max_soc_scale = 0.40

        # when the inverter is maxed out, we can't redirect battery charging power to the AC output
        # so special case above this output power
        self.solis1_max_output_power = 5700

        # Export target: center export power on this value
        # (multiply by battery SOC)
        self.p_export_target_base       = 100
        self.p_export_target_soc_factor = 1

        self.initialized = False
        self.timeout_export = Timeout( 4, expired=True )    # expires if we've been exporting for some time
        self.timeout_import = Timeout( 3, expired=True )    # expires if we've been importing for some time
        self.resend_tick = Metronome( 10 )
        self.counter = 0

    async def stop( self ):
        await mgr.evse.stop()
        for d in self.devices:
            d.off()

    async def route( self ):
        # return
        if self.initialized < 2:
            for d in self.devices:
                d.off()
            self.initialized += 1
            return

        # Routing behaves as an integrator, for stability.

        # Get export power from smartmeter, set it to positive when exporting, makes the algo below easier to understand.
        # (Smartmeter gives a negative value when exporting power, so invert the sign.)
        # This is the main control variable for power routing. The rest of this code is about tweaking it
        # according to operating conditions to get desired behavior.
        meter_export = -mgr.meter.total_power_tweaked

        # Also get solis1 output power, flip sign to make it positive when producing power
        solis1_output = -(mgr.solis1.local_meter.active_power.value or 0)

        # Smartmeter power alone is not sufficient: at night when running on battery it will fluctuate
        # around zero but with spikes in import/export and we don't want that to trigger routing!
        # What we're interested in is excess production: (meter export) + (solar battery charging power)
        # this behaves correctly in all cases.
        bp  = mgr.solis1.battery_power.value or 0       # Battery charging power. Positive if charging, negative if discharging.
        soc = mgr.solis1.bms_battery_soc.value or 0     # battery SOC, 0-100

        # correct battery power measurement offset when battery is fully charged
        if not mgr.solis1.battery_dcdc_active and -250 < bp < 200:
            bp = 0
        else:
            # use fast proxy instead
            bp = mgr.solis1.input_power

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
        #   loop becomes much slower. A few seconds delay is inserted into the loop, which may cause damped oscillations 
        #   at each load step for up to 10s, depending on the meter setting. Acrel 3 phase seems to have the best
        #   behaviour. When oscillations occur, this makes meter_export look like a mess as it oscillates around zero. 
        #   Battery power also suffers from damped oscillations.
        #     The main problem in this mode is our control variables (battery_power and meter_export) are no longer
        #   usable to make routing decisions quickly due to the oscillations.
        #   -> meter_export and battery_power should be smoothed generously before being used to make routing decisions
        #   -> Routing should be done slowly, waiting until the oscillations settle before making another decision.
        #   Special case: if the battery charger is maxed out, then switching loads off will not cause oscillations and
        #   we can do so quickly. Switching loads on may cause the inverter to redirect power from charging to grid port,
        #   if this causes the battery charger to no longer be maxed out, then oscillations will come back.

        #
        #   From the above, we distinguish two routing modes: fast and slow.
        #
        fast_route = not mgr.solis1.battery_dcdc_active
        fast_route = True

        # set desired average export power, tweak it a bit considering battery soc
        meter_export -= self.p_export_target_base + self.p_export_target_soc_factor * soc

        # Store it in a deque to smooth it, to avoid jerky behavior on power spikes
        self.smooth_export.append( meter_export )          # TODO: this was previously meter_export
        self.smooth_bp    .append( bp )
        del self.smooth_export[0]
        del self.smooth_bp    [0]

        # Smooth it, depending on the slow/fast mode
        smooth_len = - (self.smooth_length_fast if fast_route else self.smooth_length_slow)
        export_avg = average(self.smooth_export[smooth_len:])
        bp_avg     = average(self.smooth_bp[smooth_len:])

        ######## Battery Management ########

        # Note battery power should always be taken into account for routing, especially
        # if we want to keep it charging! Because some drift always occurs, so we may divert a little bit
        # more power than we should, and the inverter will provide... to do so it will reduce battery charging power
        # until it's no longer charging...

        # Get maximum charging power the battery can take, as determined by inverter, according to BMS info
        bp_max  = (mgr.solis1.battery_max_charge_current.value or 0) * (mgr.solis1.battery_voltage.value or 0)
        
        # When SOC is 100%, Solis will not charge even if the battery requests current, so don't reserve any power
        if soc == 100:
            bp_max = 0  

        # how much power do we allocate to charging the battery, according to SOC
        bp_min = bp_max * interpolate( self.battery_min_soc, self.battery_min_soc_scale, self.battery_max_soc, self.battery_max_soc_scale, soc )

        # If the inverter's grid port is maxed out, we can't steal power from the battery, correct for this.
        steal_max = max( 0, self.solis1_max_output_power - solis1_output )

        # This is how much we can steal from battery charging. If battery is discharging, it will be negative 
        # which is what we want, since we don't want to route on battery power.
        steal_from_battery = min( bp_avg - bp_min, steal_max )

        # We now have two excess power measurements
        #   export_avg                      does not include power that can be stolen from the battery
        #   export_avg+steal_from_battery   it includes that
        # TODO: use both to also control the water heater and other stuff.
        # For now this is just for the EVSE.

        if self.tick_mqtt.ticked():
            pub = {     "excess_avg"       : export_avg + steal_from_battery,
                        "excess_avg_nobat" : export_avg
                    }
            mqtt.publish( "pv/router/", pub )

        #   Note export_avg_nobat doesn't work that well. It tends to use too much from the battery
        #   because the inverter will lower battery charging power on its own to power the EVSE, but this is
        #   not visible on the smartmeter reading!
        #   So it should only be used with a highish export threshold.


        # TODO: 
        #   - export_avg_bat works (balancing with battery)
        evse = mgr.evse
        await evse.route( export_avg + steal_from_battery, fast_route )
        return


        changed = False
        # p is positive if we're drawing from grid
        p = mgr.meter.total_power_tweaked + 100
        if p < -200:
            self.timeout_import.reset()         # we're exporting power
            if self.timeout_export.expired():   # we've been exporting for a while
                for d in self.devices:          # find something to turn on
                    if not d.is_on and d.power < -p:
                        d.on()
                        changed = True
                        self.timeout_export.reset()
                        break
        elif p > 0:
            self.timeout_export.reset()            # we're importing power
            if self.timeout_import.expired():      # we've been importing for a while
                for d in reversed( self.devices ): # find something to turn off
                    if d.is_on:
                        d.off()
                        changed = True
                        self.timeout_import.reset()
                        break

        # if changed:
            # print( "Route", [d.is_on for d in self.devices], p )

        if self.resend_tick.ticked():
            for d in self.devices:
                if d.is_on:
                    d.on()
                else:
                    d.off()




########################################################################################
#
#       Put it all together
#
########################################################################################
class SolisManager():
    def __init__( self ):
        pass

        # self.fronius = Fronius( '192.168.0.17', 1 )

    ########################################################################################
    #   Start async processes
    ########################################################################################

    def start( self ):
        if sys.version_info >= (3, 11):
            with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
            # with asyncio.Runner() as runner:
                runner.run(self.astart())
        else:
            uvloop.install()
            asyncio.run(self.astart())
        # loop = asyncio.get_event_loop()
        # loop.run_until_complete( self.astart() )

    async def astart( self ):
        main_meter_modbus = AsyncModbusSerialClient(    # open Modbus on serial port
                port            = config.COM_PORT_METER,
                timeout         = 0.5,
                retries         = config.MODBUS_RETRIES_METER,
                baudrate        = 19200,
                bytesize        = 8,
                parity          = "N",
                stopbits        = 1,
            )
        self.meter = pv.main_meter.SDM630( main_meter_modbus, modbus_addr=1, key="meter", name="SDM630 Smartmeter", mqtt=mqtt, mqtt_topic="pv/meter/", mgr=self )
        self.evse  = EVSE( main_meter_modbus )
        self.meter.router = Router()

        # Instantiate inverters and local meters

        local_meter_modbus = AsyncModbusSerialClient(
                port            = config.COM_PORT_SOLIS,
                timeout         = 0.3,
                retries         = config.MODBUS_RETRIES_METER,
                baudrate        = 9600,
                bytesize        = 8,
                parity          = "N",
                stopbits        = 1,
            )
        local_meter = pv.inverter_local_meter.SDM120( local_meter_modbus, modbus_addr=3, key="ms1", name="SDM120 Smartmeter on Solis 1", mqtt=mqtt, mqtt_topic="pv/solis1/meter/" )

        self.solis1 = pv.solis_s5_eh1p.Solis( 
                modbus=local_meter_modbus, modbus_addr = 1, 
                key = "solis1", name = "Solis 1", 
                local_meter = local_meter,
                fake_meter = FakeSmartmeter( port=config.COM_PORT_FAKE_METER1, key="fake_meter_1", name="Fake meter for Solis 1",
                                            modbus_address=1, meter_type=Acrel_1_Phase ),
                mqtt = mqtt, mqtt_topic = "pv/solis1/"
        )

        async def test_coro():
            while True:
                lm = self.solis1.local_meter
                try:
                    await lm.event_power.wait()
                    if lm.is_online:
                        meter_pub = { "house_power" :  float(int(  (self.meter.total_power.value or 0)
                                                                - (lm.active_power.value or 0) )) }
                        mqtt.publish( "pv/meter/", meter_pub )            
                except:
                    log.exception(lm.key+":")

        async def test_coro2():
            s = self.solis1
            timeout_fan_off = Timeout( 60 )
            bat_power_deque = collections.deque( maxlen=10 )
            timeout_power_on  = Timeout( 60, expired=True )
            timeout_power_off = Timeout( 600 )
            timeout_blackout  = Timeout( 120, expired=True )
            power_reg = s.rwr_power_on_off

            while True:
                try:
                    await s.event_all.wait()

                    # Blackout logic: enable backup output in case of blackout
                    blackout = s.fault_status_1_grid.bit_is_active( "No grid" ) and s.phase_a_voltage.value < 100
                    if blackout:
                        log.info( "Blackout" )
                        await s.rwr_backup_output_enabled.write_if_changed( 1 )      # enable backup output
                        await s.rwr_power_on_off         .write_if_changed( s.rwr_power_on_off.value_on )   # Turn inverter on (if it was off)
                        await s.rwr_energy_storage_mode  .write_if_changed( 0x32 )   # mode = Backup, optimal revenue, charge from grid
                        timeout_blackout.reset()
                    elif not timeout_blackout.expired():        # stay in backup mode with backup output enabled for a while
                        log.info( "Remain in backup mode for %d s", timeout_blackout.remain() )
                        timeout_power_on.reset()
                        timeout_power_off.reset()
                    else:
                        await s.rwr_backup_output_enabled.write_if_changed( 0 )      # disable backup output
                        await s.rwr_energy_storage_mode  .write_if_changed( 0x23 )   # Self use, optimal revenue, charge from grid

                    # Auto on/off: turn it off at night when the batery is below specified SOC
                    # so it doesn't keep draining it while doing nothing useful
                    inverter_is_on = power_reg.value == power_reg.value_on
                    mpptv = max( s.mppt1_voltage.value, s.mppt2_voltage.value )
                    if inverter_is_on:
                        if mpptv < config.SOLIS_TURNOFF_MPPT_VOLTAGE and s.bms_battery_soc.value <= config.SOLIS_TURNOFF_BATTERY_SOC:
                            timeout_power_on.reset()
                            if timeout_power_off.expired():
                                log.info("Powering OFF %s"%s.key)
                                await power_reg.write_if_changed( power_reg.value_off )
                            else:
                                log.info( "Power off %s in %d s", s.key, timeout_power_off.remain() )
                        else:
                            timeout_power_off.reset()
                    else:
                        if mpptv > config.SOLIS_TURNON_MPPT_VOLTAGE:                        
                            timeout_power_off.reset()
                            if timeout_power_on.expired():
                                log.info("Powering ON %s"%s.key)
                                await power_reg.write_if_changed( power_reg.value_on )
                            else:
                                log.info( "Power on %s in %d s", s.key, timeout_power_on.remain() )
                        else:
                            timeout_power_on.reset()

                    # start fan if temperature too high or average battery power high
                    bat_power_deque.append( abs(s.battery_power.value) )
                    if s.temperature.value > 40 or sum(bat_power_deque) / len(bat_power_deque) > 2000:
                        timeout_fan_off.reset()
                        s.mqtt.publish( "cmnd/plugs/tasmota_t3/", {"Power": "1"} )
                        print("Fan ON")
                    elif s.temperature.value < 35 and timeout_fan_off.expired():
                        s.mqtt.publish( "cmnd/plugs/tasmota_t3/", {"Power": "0"} )
                        print("Fan OFF")

                except (asyncio.exceptions.TimeoutError, pymodbus.exceptions.ModbusException):
                    pass
                except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
                    raise
                except:
                    log.exception(s.key+":")
                    # s = traceback.format_exc()
                    # log.error(self.key+":"+s)
                    # self.mqtt.mqtt.publish( "pv/exception", s )
                    await asyncio.sleep(5)            

        app = aiohttp.web.Application()
        app.add_routes([aiohttp.web.get('/', self.webresponse), aiohttp.web.get('/solar_api/v1/GetInverterRealtimeData.cgi', self.webresponse)])
        runner = aiohttp.web.AppRunner(app, access_log=False)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner,host=config.SOLARPI_IP,port=8080)

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task( self.solis1.fake_meter.start_server() )
                tg.create_task( self.meter.read_coroutine() )
                tg.create_task( self.solis1.read_coroutine() )
                tg.create_task( self.solis1.local_meter.read_coroutine() )
                tg.create_task( self.sysinfo_coroutine() )
                tg.create_task( self.display_coroutine() )
                tg.create_task( self.mqtt_start() )
                tg.create_task( site.start() )
                tg.create_task( test_coro() )
                tg.create_task( test_coro2() )
        except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
            print("Terminated.")

        await mqtt.mqtt.disconnect()

    ########################################################################################
    #   Web server
    ########################################################################################

    async def webresponse( self, request ):
        p = (self.solis1.pv_power.value or 0) 
            # - (self.fronius.grid_port_power.value or 0)
        p *= (self.solis1.bms_battery_soc.value or 0)*0.01
        p += min(0, -self.meter.total_power.value)    # if export

        p = (self.solis1.battery_power.value or 0) - (self.meter.total_power.value or 0)
        # print("HTTP Power:%d" % p)
        return aiohttp.web.Response( text='{"Power" : %d}' % p )

    async def mqtt_start( self ):
        await mqtt.mqtt.connect( config.MQTT_BROKER_LOCAL )

    ########################################################################################
    #   System info
    ########################################################################################

    async def sysinfo_coroutine( self ):
        prev_cpu_timings = None
        while True:
            try:
                pub = {}
                with open("/proc/stat") as f:
                    cpu_timings = [ int(_) for _ in f.readline().split()[1:] ]
                    cpu_timings = cpu_timings[3], sum(cpu_timings)  # idle time, total time
                    if prev_cpu_timings:
                        pub["cpu_load_percent"] = round( 100.0*( 1.0-(cpu_timings[0]-prev_cpu_timings[0])/(cpu_timings[1]-prev_cpu_timings[1]) ), 1 )
                    prev_cpu_timings = cpu_timings

                with open("/sys/devices/virtual/thermal/thermal_zone0/temp") as f:
                    pub["cpu_temp_c"] = round( int(f.read())*0.001, 1 )

                total, used, free = shutil.disk_usage("/")
                pub["disk_space_gb"] = round( free/2**30, 2 )
                mqtt.publish( "pv/", pub )

                await asyncio.sleep(10)
            except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
                raise
            except:
                log.error(traceback.format_exc())

    ########################################################################################
    #   Local display
    ########################################################################################

    async def display_coroutine( self ):
        while True:
            await asyncio.sleep(1)
            meter = self.meter
            ms1   = self.solis1.local_meter
            solis = self.solis1
            r = [""]

            try:
                for reg in (
                    solis.fault_status_1_grid              ,
                    solis.fault_status_2_backup            ,
                    solis.fault_status_3_battery           ,
                    solis.fault_status_4_inverter          ,
                    solis.fault_status_5_inverter          ,
                    # solis.inverter_status                  ,
                    solis.operating_status                 ,
                    # solis.energy_storage_mode              ,
                    solis.rwr_energy_storage_mode          ,
                ):
                    r.extend( reg.get_on_bits() )


                for reg in (
                    meter.phase_1_power                    ,
                    meter.phase_2_power                    ,
                    meter.phase_3_power                    ,
                    "",
                    meter.total_power               ,
                    solis.pv_power,      
                    solis.battery_power,                      
                    # self.fronius.grid_port_power,
                    ms1.active_power,
                    # solis.house_load_power,                   
                    solis.backup_load_power,                  
                    "",
                    solis.dc_bus_voltage,      
                    solis.temperature,
                    "",
                    solis.battery_voltage,                    
                    solis.bms_battery_voltage,                
                    # solis.battery_current_direction,                    
                    solis.battery_current,                    
                    solis.bms_battery_current,                
                    solis.bms_battery_charge_current_limit,   
                    solis.bms_battery_discharge_current_limit,
                    solis.bms_battery_soc,                    
                    solis.bms_battery_health_soh,   
                    solis.rwr_power_on_off,          

            #         solis.energy_generated_today,

            #         solis.phase_a_voltage,

            # solis.rwr_power_limit_switch,
            # solis.rwr_actual_power_limit_adjustment_value,

                    "",

            # solis.meter_ac_voltage_a                           ,
            # solis.meter_ac_current_a                           ,
            # solis.meter_ac_voltage_b                           ,
            # solis.meter_ac_current_b                           ,
            # solis.meter_ac_voltage_c                           ,
            # solis.meter_ac_current_c                           ,
            # solis.meter_active_power_a                         ,
            # solis.meter_active_power_b                         ,
            # solis.meter_active_power_c                         ,
            # solis.meter_total_active_power                     ,
            # solis.meter_reactive_power_a                       ,
            # solis.meter_reactive_power_b                       ,
            # solis.meter_reactive_power_c                       ,
            # solis.meter_total_reactive_power                   ,
            # solis.meter_apparent_power_a                       ,
            # solis.meter_apparent_power_b                       ,
            # solis.meter_apparent_power_c                       ,
            # solis.meter_total_apparent_power                   ,
            # solis.meter_power_factor                           ,
            # solis.meter_grid_frequency                         ,
            # solis.meter_total_active_energy_imported_from_grid ,
            # solis.meter_total_active_energy_exported_to_grid   ,


                    ):
                    if isinstance( reg, str ):
                        r.append(reg)
                    else:
                        if reg.value != None:
                            r.append( "%40s %10s %10s" % (reg.key, reg.device.key, reg.format_value() ) )

                # print( "\n".join(r) )
            except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
                raise
            except:
                log.error(traceback.format_exc())



if 1:
    mgr = SolisManager()
    try:
        mgr.start()
    finally:
        logging.shutdown()
else:
    import cProfile
    with cProfile.Profile( time.process_time ) as pr:
        pr.enable()
        try:
            mgr = SolisManager()
            mgr.start()
        finally:
            pr.dump_stats("profile.log")







