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
from pymodbus.exceptions import ModbusException
from asyncio.exceptions import TimeoutError, CancelledError

# Device wrappers and misc local libraries
import config
from misc import *
from pv.mqtt_wrapper import MQTTWrapper
import grugbus
from grugbus.devices import Eastron_SDM120, Eastron_SDM630, Acrel_1_Phase, Acrel_ACR10RD16TE4
import pv.solis_s5_eh1p, pv.inverter_local_meter, pv.main_meter, pv.evse_abb_terra, pv.fake_meter

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

        self.start_timeout = Timeout( 5, 3, expired=False )

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

        # Wait for it to stabilize at startup
        if not self.start_timeout.expired():
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

        export_avg_bat =  export_avg + steal_from_battery
        if self.tick_mqtt.ticked():
            pub = {     "excess_avg"       : export_avg_bat,
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
        await evse.route( export_avg_bat, fast_route )
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


async def log_coroutine( title, fut ):
    log.info("Start:"+title )
    try:
        await fut
    finally:
        log.info("Exit: "+title )


########################################################################################
#
#       Put it all together
#
########################################################################################
class SolisManager():
    def __init__( self ):
        pass

    ########################################################################################
    #
    #   Blackout logic
    #
    ########################################################################################
    async def inverter_blackout_coroutine( self, solis ):
        timeout_blackout  = Timeout( 120, expired=True )
        while True:
            await solis.event_all.wait()
            try:
                # Blackout logic: enable backup output in case of blackout
                blackout = solis.fault_status_1_grid.bit_is_active( "No grid" ) and solis.phase_a_voltage.value < 100
                if blackout:
                    log.info( "Blackout" )
                    await solis.rwr_backup_output_enabled.write_if_changed( 1 )      # enable backup output
                    await solis.rwr_power_on_off         .write_if_changed( solis.rwr_power_on_off.value_on )   # Turn inverter on (if it was off)
                    await solis.rwr_energy_storage_mode  .write_if_changed( 0x32 )   # mode = Backup, optimal revenue, charge from grid
                    timeout_blackout.reset()
                elif not timeout_blackout.expired():        # stay in backup mode with backup output enabled for a while
                    log.info( "Remain in backup mode for %d s", timeout_blackout.remain() )
                    timeout_power_on.reset()
                    timeout_power_off.reset()
                else:
                    await solis.rwr_backup_output_enabled.write_if_changed( 0 )      # disable backup output
                    await solis.rwr_energy_storage_mode  .write_if_changed( 0x23 )   # Self use, optimal revenue, charge from grid

            except (TimeoutError, ModbusException): pass
            except Exception:
                log.exception(solis.key+":")
                await asyncio.sleep(5)           

    ########################################################################################
    #
    #   Low battery power save logic
    #
    ########################################################################################
    async def inverter_powersave_coroutine( self, solis ):
        timeout_power_on  = Timeout( 60, expired=True )
        timeout_power_off = Timeout( 600 )
        power_reg = solis.rwr_power_on_off
        while True:
            await solis.event_all.wait()
            try:
                # Auto on/off: turn it off at night when the battery is below specified SOC
                # so it doesn't keep draining it while doing nothing useful
                inverter_is_on = power_reg.value == power_reg.value_on
                mpptv = max( solis.mppt1_voltage.value, solis.mppt2_voltage.value )
                if inverter_is_on:
                    if mpptv < config.SOLIS_TURNOFF_MPPT_VOLTAGE and solis.bms_battery_soc.value <= config.SOLIS_TURNOFF_BATTERY_SOC:
                        timeout_power_on.reset()
                        if timeout_power_off.expired():
                            log.info("Powering OFF %s"%solis.key)
                            await power_reg.write_if_changed( power_reg.value_off )
                        else:
                            log.info( "Power off %s in %d s", solis.key, timeout_power_off.remain() )
                    else:
                        timeout_power_off.reset()
                else:
                    if mpptv > config.SOLIS_TURNON_MPPT_VOLTAGE:                        
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
                log.exception(solis.key+":")
                await asyncio.sleep(5)      

    ########################################################################################
    #
    #   start fan if temperature too high or average battery power high
    #
    ########################################################################################
    async def inverter_fan_coroutine( self, solis ):
        timeout_fan_off = Timeout( 60 )
        bat_power_deque = collections.deque( maxlen=10 )
        while True:
            await solis.event_all.wait()
            try:
                bat_power_deque.append( abs(solis.battery_power.value) )
                if solis.temperature.value > 40 or sum(bat_power_deque) / len(bat_power_deque) > 2000:
                    timeout_fan_off.reset()
                    solis.mqtt.publish( "cmnd/plugs/tasmota_t3/", {"Power": "1"} )
                elif solis.temperature.value < 35 and timeout_fan_off.expired():
                    solis.mqtt.publish( "cmnd/plugs/tasmota_t3/", {"Power": "0"} )

            except (TimeoutError, ModbusException): pass
            except:
                log.exception(solis.key+":")
                await asyncio.sleep(5)           

    ########################################################################################
    #
    #   Compute house power
    #
    ########################################################################################
    async def house_power_coroutine( self ):
        lm = self.solis1.local_meter
        while True:
            await lm.event_power.wait()
            try:
                if lm.is_online:
                    meter_pub = { "house_power" :  float(int(  (self.meter.total_power.value or 0)
                                                            - (lm.active_power.value or 0) )) }
                    mqtt.publish( "pv/meter/", meter_pub )            
            except (TimeoutError, ModbusException): pass
            except:
                log.exception(lm.key+":")

    ########################################################################################
    #
    #   Fake Meter
    #
    ########################################################################################
    async def fake_meter_coroutine( self ):
        m = self.meter
        await m.event_all.wait()
        step_offset  = 0.
        prev_power   = 0.
        while True:
            await m.event_power.wait()
            try:                            
                # offset measured power a little bit to ensure a small value of export
                m.total_power_tweaked = m.total_power.value
                bp  = self.solis1.battery_power.value or 0       # Battery charging power. Positive if charging, negative if discharging.
                soc = self.solis1.bms_battery_soc.value or 0     # battery soc, 0-100
                if bp > 200:        m.total_power_tweaked += soc*bp*0.0001

                # delta = m.total_power_tweaked - prev_power
                # prev_power = m.total_power_tweaked

                # if abs( delta ) > 400:
                #     step_offset += delta*1.25

                # if abs( step_offset ) > 10:
                #     print( "+", step_offset )
                # fake_power = m.total_power_tweaked + step_offset
                # step_offset *= 0.75
                fake_power = m.total_power_tweaked

                #   Fill fakemeter fields
                fm = self.solis1.fake_meter
                fm.voltage                .value = m.phase_1_line_to_neutral_volts .value
                fm.current                .value = m.phase_1_current               .value
                fm.apparent_power         .value = m.total_volt_amps               .value
                fm.reactive_power         .value = m.total_var                     .value
                fm.power_factor           .value =(m.total_power_factor            .value % 1.0)
                fm.frequency              .value = m.frequency                     .value
                fm.import_active_energy   .value = m.total_import_kwh              .value
                fm.export_active_energy   .value = m.total_export_kwh              .value
                fm.active_power           .value = fake_power
                fm.data_timestamp = m.last_transaction_timestamp
                fm.is_online = m.is_online
                fm.write_regs_to_context()
            except Exception:
                log.exception("Fill fake meter:")

    #
    #   Async entry point
    #
    def start( self ):
        with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
            runner.run(self.astart())

    #
    #   Build hardware
    #
    async def astart( self ):
        await mqtt.mqtt.connect( config.MQTT_BROKER_LOCAL )
        while not mqtt.is_connected:
            await asyncio.sleep(0.1)

        #
        #   Open RS485 ports
        #
        main_meter_modbus = AsyncModbusSerialClient(    # open Modbus on serial port
                port            = config.COM_PORT_METER,        timeout         = 0.5,
                retries         = config.MODBUS_RETRIES_METER,  baudrate        = 19200,
                bytesize        = 8,    parity          = "N",  stopbits        = 1,
            )
        local_meter_modbus = AsyncModbusSerialClient(
                port            = config.COM_PORT_SOLIS,        timeout         = 0.3,
                retries         = config.MODBUS_RETRIES_METER,  baudrate        = 9600,
                bytesize        = 8,    parity          = "N",  stopbits        = 1,
            )

        #
        #   Instantiate hardware objects
        #
        self.meter = pv.main_meter.SDM630( main_meter_modbus, modbus_addr=1, key="meter", name="SDM630 Smartmeter", mqtt=mqtt, mqtt_topic="pv/meter/", mgr=self )
        self.evse  = pv.evse_abb_terra.EVSE( main_meter_modbus, modbus_addr=3, key="evse", name="ABB Terra", mqtt=mqtt, mqtt_topic="pv/evse/" )

        self.meter.router = Router()

        # Instantiate inverters and local meters

        local_meter = pv.inverter_local_meter.SDM120( local_meter_modbus, modbus_addr=3, key="ms1", name="SDM120 Smartmeter on Solis 1", mqtt=mqtt, mqtt_topic="pv/solis1/meter/" )

        self.solis1 = pv.solis_s5_eh1p.Solis( 
                modbus=local_meter_modbus,      modbus_addr = 1,       key = "solis1", name = "Solis 1",    local_meter = local_meter,
                fake_meter = pv.fake_meter.FakeSmartmeter( port=config.COM_PORT_FAKE_METER1, key="fake_meter_1", name="Fake meter for Solis 1",modbus_address=1, meter_type=Acrel_1_Phase ),
                mqtt = mqtt, mqtt_topic = "pv/solis1/"
        )
 
        app = aiohttp.web.Application()
        app.add_routes([aiohttp.web.get('/', self.webresponse), aiohttp.web.get('/solar_api/v1/GetInverterRealtimeData.cgi', self.webresponse)])
        runner = aiohttp.web.AppRunner(app, access_log=False)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner,host=config.SOLARPI_IP,port=8080)
        await site.start()

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task( log_coroutine( "server: solis1 fakemeter",  self.solis1.fake_meter.start_server() ))
                tg.create_task( log_coroutine( "read: main meter",          self.meter.read_coroutine() ))
                tg.create_task( log_coroutine( "read: solis1",              self.solis1.read_coroutine() ))
                tg.create_task( log_coroutine( "read: solis1 local meter",  self.solis1.local_meter.read_coroutine() ))
                tg.create_task( log_coroutine( "logic:fan",                 self.inverter_fan_coroutine( self.solis1 ) ))
                tg.create_task( log_coroutine( "logic:blackout",            self.inverter_blackout_coroutine( self.solis1 ) ))
                tg.create_task( log_coroutine( "logic:house_power",         self.house_power_coroutine( ) ))
                tg.create_task( log_coroutine( "logic:fill fake meter",     self.fake_meter_coroutine( ) ))
                tg.create_task( log_coroutine( "sysinfo",                   self.sysinfo_coroutine() ))
        except (KeyboardInterrupt, CancelledError):
            print("Terminated.")
        finally:
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
            except Exception:
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







