#!/usr/bin/python
# -*- coding: utf-8 -*-

import os, time, sys, serial, socket, logging, logging.handlers, shutil, importlib, orjson
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
import config
from misc import *
from pv.mqtt_wrapper import MQTTWrapper, MQTTSetting, MQTTVariable
import grugbus
from grugbus.devices import Eastron_SDM120, Eastron_SDM630, Acrel_1_Phase, Acrel_ACR10RD16TE4
import pv.solis_s5_eh1p, pv.meters, pv.evse_abb_terra


"""
    python3.11
    pymodbus 3.7.x

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
                     handlers=[
                            logging.handlers.RotatingFileHandler(Path(__file__).stem+'.log', mode='a', maxBytes=5*1024*1024, backupCount=2, encoding=None, delay=False),
                            # logging.FileHandler(filename=Path(__file__).stem+'.log'), 
                            logging.StreamHandler(stream=sys.stdout)
                    ])
log = logging.getLogger(__name__)


class Routable():
    def __init__( self, name, power ):
        self.name = name
        self.power = power
        self.is_on = None
        self.off()

class RoutableTasmota( Routable ):
    def __init__( self, name, power, mqtt, mqtt_prefix ):
        self.mqtt = mqtt
        self.mqtt_prefix = mqtt_prefix
        super().__init__( name, power )

    def mqtt_power( self, message ):
        return self.mqtt.publish( "cmnd/%s/Power"%self.mqtt_prefix, message )

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

class Router( ):
    def __init__( self, mqtt, mqtt_topic ):
        self.mqtt        = mqtt
        self.mqtt_topic  = mqtt_topic

        self.devices = [ 
                         RoutableTasmota("Tasmota T4 Sèche serviette"   , 1050 , self.mqtt, "plugs/tasmota_t4"),
                         RoutableTasmota("Tasmota T2 Radiateur PF"      , 800  , self.mqtt, "plugs/tasmota_t2"),
                         RoutableTasmota("Tasmota T1 Radiateur bureau"  , 1700 , self.mqtt, "plugs/tasmota_t1"),
                        ]       

        # queues to smooth measured power
        self.smooth_export = MovingAverage( 1 )
        self.smooth_bp     = MovingAverage( 1 )
        self.charge_detect_avg = MovingAverage( 20 )

        # Battery charging current is limited to the product of:
        #   1) Full charging current (as requested by BMS depending on SOC)
        #   2) Scale factor below, from 0 to 1
        #   Note: when inverter is hot or soc>90%, battery charging is reduced to 85A and sometimes 
        #   this does not show up in battery_max_charge_current, so adjust below parameters to avoid
        #   wasting power

        MQTTSetting( self, "battery_min_soc"        , int   , lambda x: (0<=x<=100), 85 )   # 
        MQTTSetting( self, "battery_max_soc"        , int   , lambda x: (0<=x<=100), 90 )   # 
        MQTTSetting( self, "battery_min_soc_scale"  , float , lambda x: (0<=x<=100), 0.98 ) #
        MQTTSetting( self, "battery_max_soc_scale"  , float , lambda x: (0<=x<=100), 0.20 ) #

        # Export target: center export power on this value
        # (multiply by battery SOC)
        MQTTSetting( self, "p_export_target_base_W"      , float , lambda x: (-200<=x<=200), 100 ) #
        MQTTSetting( self, "p_export_target_soc_factor"  , float , lambda x: (0<=x<=2), 1.0 ) #

        # when the inverter is maxed out, we can't redirect battery charging power to the AC output
        # so special case above this output power
        self.solis1_max_output_power = 5700

        self.initialized = False
        # self.timeout_export = Timeout( 4, expired=True )    # expires if we've been exporting for some time
        # self.timeout_import = Timeout( 3, expired=True )    # expires if we've been importing for some time
        # self.resend_tick = Metronome( 10 )
        # self.counter = 0

        # self.start_timeout = Timeout( 5, expired=False )

    async def stop( self ):
        await mgr.evse.pause_charge()
        for d in self.devices:
            d.off()

    async def route_coroutine( self ):
        try:
            # wait for startup transient to pass
            await asyncio.sleep(5)
            await mgr.event_power.wait()
            log.info("Routing enabled.")
            await mgr.evse.initialize()

            while True:
                await mgr.event_power.wait()                
                try:
                    pass
                    await self.route()

                except Exception:
                    log.exception("Router:")
                    await asyncio.sleep(1)
        finally:
            await self.stop()


    async def route( self ):
        # return
        if self.initialized < 2:
            for d in self.devices:
                d.off()
            self.initialized += 1
            return

        # Routing behaves as an integrator, for stability.

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


        # Get export power from smartmeter, set it to positive when exporting (invert sign), makes the algo below easier to understand.
        # Store it in a deque to smooth it, to avoid jerky behavior on power spikes
        export_avg = self.smooth_export.append( -mgr.meter_power_tweaked )          # TODO: this was previously meter_export

        # Smartmeter power alone is not sufficient: at night when running on battery it will fluctuate
        # around zero but with spikes in import/export and we don't want that to trigger routing!
        # What we're interested in is excess production: (meter export) + (solar battery charging power)
        # this behaves correctly in all cases.
        # Get battery info
        bp_proxy  = mgr.total_input_power      # Battery charging power (fast proxy from smartmeter). Positive if charging, negative if discharging.
        soc = mgr.bms_soc.value            # battery SOC, 0-100

        # Detect "battery_full" which really means "inverter doesn't want to charge it"
        # in this case, we must not reserve power for charging, because the inverter won't use it!
        charge_detect = self.charge_detect_avg.append( mgr.bms_current.value > 1 ) 
        charge_detect = (charge_detect or 0) > 0.1
        battery_full = (mgr.battery_max_charge_power==0) or (soc >= 98 and not charge_detect)


        # remove battery power measurement error when battery is fully charged
        if battery_full:
            bp_proxy = 0
        bp_avg     = self.smooth_bp    .append( bp_proxy )

        # Wait for moving average to fill
        if export_avg is None or bp_avg is None:
            return

        # tweak it a bit to keep a little bit of extra power
        export_avg -= self.p_export_target_base_W.value + self.p_export_target_soc_factor.value * soc
    
        ######## Battery Management ########

        # Note battery power should always be taken into account for routing, especially
        # if we want to keep it charging! Because some drift always occurs, so we may divert a little bit
        # more power than we should, and the inverter will provide... to do so it will reduce battery charging power
        # until it's no longer charging...
        # Get maximum charging power the battery can take, as determined by inverter, according to BMS info
        if battery_full:    bp_max  = 0
        else:               bp_max = mgr.battery_max_charge_power
        
        # how much power do we allocate to charging the battery, according to SOC
        # if EV charging has begun, pretend we have more SOC than we have to avoid start/stop cycles
        if mgr.evse.is_charge_unpaused():
            soc += 5
        bp_min = bp_max * interpolate( self.battery_min_soc.value, self.battery_min_soc_scale.value, self.battery_max_soc.value, self.battery_max_soc_scale.value, soc )

        # # If the inverter's grid port is maxed out, we can't steal power from the battery, correct for this.
        # steal_max = max( 0, self.solis1_max_output_power - solis1_output )

        # # This is how much we can steal from battery charging. If battery is discharging, it will be negative 
        # # which is what we want, since we don't want to route on battery power.
        
        # steal_from_battery = min( bp_avg - bp_min, steal_max )
        steal_from_battery = bp_avg - bp_min

        # We now have two excess power measurements
        #   export_avg                      does not include power that can be stolen from the battery
        #   export_avg+steal_from_battery   it includes that
        # TODO: use both to also control the water heater and other stuff.
        # For now this is just for the EVSE.

        export_avg_bat =  export_avg + steal_from_battery
        # mqtt.publish_value( "pv/router/excess_avg_nobat", export_avg )
        self.mqtt.publish_value( "pv/router/battery_min_charge_power",  bp_min, int )
        self.mqtt.publish_value( "pv/router/excess_avg", export_avg_bat, int )

        #   Note export_avg_nobat doesn't work that well. It tends to use too much from the battery
        #   because the inverter will lower battery charging power on its own to power the EVSE, but this is
        #   not visible on the main smartmeter reading!
        #   So it should only be used with a highish export threshold.

        evse = mgr.evse
        await evse.route( export_avg_bat )
        return

    #   Old version
    #
    # async def route( self ):
    #     # return
    #     if self.initialized < 2:
    #         for d in self.devices:
    #             d.off()
    #         self.initialized += 1
    #         return

    #     # Wait for it to stabilize at startup
    #     if not self.start_timeout.expired():
    #         return

    #     # Routing behaves as an integrator, for stability.

    #     # Get export power from smartmeter, set it to positive when exporting, makes the algo below easier to understand.
    #     # (Smartmeter gives a negative value when exporting power, so invert the sign.)
    #     # This is the main control variable for power routing. The rest of this code is about tweaking it
    #     # according to operating conditions to get desired behavior.
    #     meter_export = -mgr.meter_power_tweaked

    #     # Also get solis1 output power, flip sign to make it positive when producing power
    #     solis1_output = -(mgr.solis1.local_meter.active_power.value or 0)

    #     # Smartmeter power alone is not sufficient: at night when running on battery it will fluctuate
    #     # around zero but with spikes in import/export and we don't want that to trigger routing!
    #     # What we're interested in is excess production: (meter export) + (solar battery charging power)
    #     # this behaves correctly in all cases.
    #     bp  = mgr.solis1.battery_power.value or 0       # Battery charging power. Positive if charging, negative if discharging.
    #     soc = mgr.solis1.bms_soc.value or 0     # battery SOC, 0-100

    #     # correct battery power measurement offset when battery is fully charged
    #     if not mgr.solis1.battery_dcdc_active.value and -250 < bp < 200:
    #         bp = 0
    #     else:
    #         # use fast proxy instead
    #         bp = mgr.solis1.input_power.value

    #     # Solis S5 EH1P has several different behaviors to which we should adapt for better routing.
    #     #
    #     # 1) Feedback loop inoperative, PV controls the output
    #     #   Full export: (meter_export > 0) AND (Battery is full, battery_max_charge_current = 0)
    #     #   The inverver is exporting everything it can. Routing power will change meter_export but the inverter 
    #     #   won't react to that unless we draw too much, causing meter_export to become negative, at this point 
    #     #   it will retake control and draw power from the battery, exiting this mode.
    #     #   -> The router is actually in control of everything
    #     #   -> We can route as fast as possible, but keep meter_export > 0
    #     #
    #     # 1a) EPM Export power limit (per inverter setting)
    #     #   Feedback loop operating. Inverter controls export power.
    #     #   In this mode, the feedback loop is very quick: it reads the meter once per second and reacts almost
    #     #   immediately, taking more power from PV if available.
    #     #   -> Again, route as fast as possible, taking into account the inverter will react one second later.
    #     #   
    #     # 2) Inverter grid port is maxed out
    #     #   The inverter gets more power from PV than its grid port can export. This power can be used to charge 
    #     #   the battery, if needed. This mode is relevant for routing because while it would appear we can steal
    #     #   power from the battery (because we see it is charging), in reality we can't because the inverter 
    #     #   can't output more power on the grid port. So any extra load being switched on would draw from the grid.
    #     #   -> Detect this and avoid this mistake
    #     #   -> Inverter is maxed out, so the router is actually in control of everything
    #     #
    #     # 3) Battery DC-DC is operating
    #     #     Unless the battery DC-DC is maxed out, this corresponds to meter_export being near zero.
    #     #     When the inverter is working with the battery (doesn't matter if charging or discharging) the control 
    #     #   loop becomes much slower. A few seconds delay is inserted into the loop, which may cause damped oscillations 
    #     #   at each load step for up to 10s, depending on the meter setting. Acrel 3 phase seems to have the best
    #     #   behaviour. When oscillations occur, this makes meter_export look like a mess as it oscillates around zero. 
    #     #   Battery power also suffers from damped oscillations.
    #     #     The main problem in this mode is our control variables (battery_power and meter_export) are no longer
    #     #   usable to make routing decisions quickly due to the oscillations.
    #     #   -> meter_export and battery_power should be smoothed generously before being used to make routing decisions
    #     #   -> Routing should be done slowly, waiting until the oscillations settle before making another decision.
    #     #   Special case: if the battery charger is maxed out, then switching loads off will not cause oscillations and
    #     #   we can do so quickly. Switching loads on may cause the inverter to redirect power from charging to grid port,
    #     #   if this causes the battery charger to no longer be maxed out, then oscillations will come back.

    #     #
    #     #   From the above, we distinguish two routing modes: fast and slow.
    #     #
    #     fast_route = not mgr.solis1.battery_dcdc_active.value
    #     fast_route = True

    #     # set desired average export power, tweak it a bit considering battery soc
    #     meter_export -= self.p_export_target_base + self.p_export_target_soc_factor * soc

    #     # Store it in a deque to smooth it, to avoid jerky behavior on power spikes
    #     self.smooth_export.append( meter_export )          # TODO: this was previously meter_export
    #     self.smooth_bp    .append( bp )
    #     del self.smooth_export[0]
    #     del self.smooth_bp    [0]

    #     # Smooth it, depending on the slow/fast mode
    #     smooth_len = - (self.smooth_length_fast if fast_route else self.smooth_length_slow)
    #     export_avg = average(self.smooth_export[smooth_len:])
    #     bp_avg     = average(self.smooth_bp[smooth_len:])

    #     ######## Battery Management ########

    #     # Note battery power should always be taken into account for routing, especially
    #     # if we want to keep it charging! Because some drift always occurs, so we may divert a little bit
    #     # more power than we should, and the inverter will provide... to do so it will reduce battery charging power
    #     # until it's no longer charging...

    #     # Get maximum charging power the battery can take, as determined by inverter, according to BMS info
    #     bp_max  = (mgr.solis1.battery_max_charge_current.value or 0) * (mgr.solis1.battery_voltage.value or 0)
        
    #     # When SOC is 100%, Solis will not charge even if the battery requests current, so don't reserve any power
    #     if soc == 100:
    #         bp_max = 0  

    #     # how much power do we allocate to charging the battery, according to SOC
    #     bp_min = bp_max * interpolate( self.battery_min_soc, self.battery_min_soc_scale, self.battery_max_soc, self.battery_max_soc_scale, soc )

    #     # If the inverter's grid port is maxed out, we can't steal power from the battery, correct for this.
    #     steal_max = max( 0, self.solis1_max_output_power - solis1_output )

    #     # This is how much we can steal from battery charging. If battery is discharging, it will be negative 
    #     # which is what we want, since we don't want to route on battery power.
    #     steal_from_battery = min( bp_avg - bp_min, steal_max )

    #     # We now have two excess power measurements
    #     #   export_avg                      does not include power that can be stolen from the battery
    #     #   export_avg+steal_from_battery   it includes that
    #     # TODO: use both to also control the water heater and other stuff.
    #     # For now this is just for the EVSE.

    #     export_avg_bat =  export_avg + steal_from_battery
    #     mqtt.publish_value( "pv/router/excess_avg", export_avg_bat )

    #     #   Note export_avg_nobat doesn't work that well. It tends to use too much from the battery
    #     #   because the inverter will lower battery charging power on its own to power the EVSE, but this is
    #     #   not visible on the smartmeter reading!
    #     #   So it should only be used with a highish export threshold.


    #     # TODO: 
    #     #   - export_avg_bat works (balancing with battery)
    #     evse = mgr.evse
    #     await evse.route( export_avg_bat, fast_route )
    #     return


        changed = False
        # p is positive if we're drawing from grid
        p = mgr.meter.meter_power_tweaked + 100
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

#   Write log when a coroutine enters/exits
#

########################################################################################
#
#       Put it all together
#
########################################################################################
class Master():
    def __init__( self ):
        self.event_power = asyncio.Event()
        self.event_meter_update = asyncio.Event()

        self.meter_power_tweaked      = 0    
        self.house_power              = 0    
        self.total_pv_power           = 0    # Total PV production reported by inverters. Fast, but inaccurate.
        self.total_input_power        = 0    # Power going into the inverter/battery (PV and grid port). Fast proxy for battery charging power. For routing.
        self.total_grid_port_power    = 0    # Sum of inverters local smartmeter power (negative=export)
        self.total_battery_power      = 0    # Battery power for both inverters (positive for charging)
        self.battery_max_charge_power = 0    

    #
    #   Build hardware
    #
    async def astart( self ):
        self.mqtt = MQTTWrapper( "pv_master" )
        self.mqtt_topic = "pv/"
        await self.mqtt.mqtt.connect( config.MQTT_BROKER_LOCAL )

        # Get battery current from BMS
        MQTTVariable( "pv/bms/current", self, "bms_current", float, None, 0 )
        MQTTVariable( "pv/bms/power",   self, "bms_power",   float, None, 0 )
        MQTTVariable( "pv/bms/soc",     self, "bms_soc",     float, None, 0 )

        # Get information from Controller
        MQTTVariable( "pv/meter/total_power"                   , self, "meter_total_power"                  , float, None, 0, self.mqtt_update_meter_callback )
        MQTTVariable( "pv/meter/phase_1_line_to_neutral_volts" , self, "meter_phase_1_line_to_neutral_volts", float, None, 0 )
        MQTTVariable( "pv/meter/phase_2_line_to_neutral_volts" , self, "meter_phase_2_line_to_neutral_volts", float, None, 0 )
        MQTTVariable( "pv/meter/phase_3_line_to_neutral_volts" , self, "meter_phase_3_line_to_neutral_volts", float, None, 0 )
        MQTTVariable( "pv/meter/phase_1_current"               , self, "meter_phase_1_current"              , float, None, 0 )
        MQTTVariable( "pv/meter/phase_2_current"               , self, "meter_phase_2_current"              , float, None, 0 )
        MQTTVariable( "pv/meter/phase_3_current"               , self, "meter_phase_3_current"              , float, None, 0 )

        #
        #   EVSE and its smartmeter, both on the same modbus port
        #
        modbus_evse = AsyncModbusSerialClient( **config.EVSE["SERIAL"] )
        self.evse  = pv.evse_abb_terra.EVSE( 
            modbus_evse,
            **config.EVSE["PARAMS"], 
            local_meter = pv.meters.SDM120( 
                modbus_evse,
                mqtt       = self.mqtt,
                mqtt_topic = "pv/evse/meter/" ,
                **config.EVSE["LOCAL_METER"]["PARAMS"], 
            ),
            mqtt        = self.mqtt, 
            mqtt_topic  = "pv/evse/" 
        )

        # add voltage to EVSE meter register poll list
        self.evse.local_meter.reg_sets[0].append( self.evse.local_meter.voltage )

        #
        #   Solis inverters and local meters
        #
        self.inverters = [
            pv.solis_s5_eh1p.Solis( 
                AsyncModbusSerialClient( **cfg["SERIAL"] ),
                local_meter = pv.meters.SDM120( 
                    AsyncModbusSerialClient( **cfg["LOCAL_METER"]["SERIAL"] ),
                    mqtt       = self.mqtt,
                    mqtt_topic ="pv/%s/meter/" % key,
                    **cfg["LOCAL_METER"]["PARAMS"],
                ),
                fake_meter_type      = Acrel_1_Phase,
                fake_meter_placement = "grid", 
                mqtt                 = self.mqtt,
                mqtt_topic           = "pv/%s/" % key,
                **cfg["PARAMS"],
            )        
            for key, cfg in config.SOLIS.items()
        ]
        for v in self.inverters:
            setattr( self, v.key, v )

        self.router = Router( mqtt = self.mqtt, mqtt_topic = "pv/router/" )

        try:
            async with asyncio.TaskGroup() as tg:
                for v in self.inverters + [self.evse]:
                    tg.create_task( self.log_coroutine( "read: %s"             %v.key, v.read_coroutine() ))
                    tg.create_task( self.log_coroutine( "read: %s local meter" %v.key, v.local_meter.read_coroutine() ))
                for v in self.inverters:
                    tg.create_task( self.log_coroutine( "powersave: %s" % v.key, self.inverter_powersave_coroutine( v ) ))

                tg.create_task( self.log_coroutine( "logic: fan",                self.inverter_fan_coroutine( ) ))
                # tg.create_task( self.log_coroutine( "logic: blackout",           self.inverter_blackout_coroutine( self.solis1 ) ))
                tg.create_task( self.log_coroutine( "logic: power calculations", self.power_coroutine( ) ))
                tg.create_task( self.log_coroutine( "logic: router",             self.router.route_coroutine( ) ))
                tg.create_task( self.log_coroutine( "Reload python modules",     self.reload_coroutine() ))
        except (KeyboardInterrupt, CancelledError):
            print("Terminated.")
        finally:
            await self.mqtt.mqtt.disconnect()
            with open("mqtt_stats/pv_master.txt","w") as f:
                self.mqtt.write_stats( f )

    ########################################################################################
    #
    #   Compute power values and fill fake meter fields
    #
    ########################################################################################
    async def mqtt_update_meter_callback( self, param ):
        self.event_meter_update.set()
        self.event_meter_update.clear()

    async def power_coroutine( self ):
        await asyncio.sleep(2)      # wait for Solis to read all its registers (or fail if it is offline)

        while True:
            try:
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

                for solis in self.inverters:
                    if len( inverters_online ) == 1:
                        fake_power = meter_power_tweaked
                    else:
                        # balance power between inverters
                        # if len( inverters_with_battery ) == 2:
                        #     fake_power = ( meter_power_tweaked * 0.5 
                        #                     + 0.05*(solis.battery_power.value - total_battery_power*0.5)
                        #                     - 0.01*(solis.pv_power.value - total_pv_power*0.5) )
                        # else:
                        fake_power = meter_power_tweaked * 0.5 + 0.05*(solis.input_power.value - total_input_power*0.5)
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
    async def inverter_powersave_coroutine( self, solis ):
        timeout_power_on  = Timeout( 60, expired=True )
        timeout_power_off = Timeout( 300 )
        power_reg = solis.rwr_power_on_off
        while True:
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
    async def inverter_fan_coroutine( self ):
        timeout_fan_off = Timeout( 60 )
        bat_power_avg = { _.key: MovingAverage(10) for _ in self.inverters }
        await asyncio.sleep( 5 )
        while True:
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

    #
    #   Async entry point
    #
    def start( self ):
        with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
            runner.run(self.astart())

    ########################################################################################
    #
    #       Reload code when changed
    #
    ########################################################################################
    async def reload_coroutine( self ):
        mtimes = {}

        while True:
            for module in config,:
                await asyncio.sleep(1)
                try:
                    fname = Path( module.__file__ )
                    mtime = fname.mtime
                    if (old_mtime := mtimes.get( fname )) and old_mtime < mtime:
                        log.info( "Reloading: %s", fname )
                        importlib.reload( module )
                        if module is config:
                            self.mqtt.load_rate_limit() # reload rate limit configuration

                    mtimes[ fname ] = mtime
                except Exception:
                    log.exception("Reload coroutine:")

    async def log_coroutine( self, title, fut ):
        log.info("Start:"+title )
        try:        await fut
        finally:    log.info("Exit: "+title )

if 1:
    try:
        mgr = Master()
        mgr.start()
    finally:
        logging.shutdown()
else:
    import cProfile
    with cProfile.Profile( time.process_time ) as pr:
        pr.enable()
        try:
            mgr = Master()
            mgr.start()
        finally:
            logging.shutdown()
            pr.dump_stats("profile.dump")







