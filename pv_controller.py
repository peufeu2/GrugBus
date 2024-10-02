#!/usr/bin/python
# -*- coding: utf-8 -*-

import sys, time, serial, socket, logging, logging.handlers, traceback, shutil, uvloop, asyncio, orjson, importlib
from path import Path
from asyncio.exceptions import TimeoutError, CancelledError

# Modbus
import pymodbus
from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient
from pymodbus.datastore import ModbusServerContext, ModbusSlaveContext, ModbusSequentialDataBlock
from pymodbus.server import StartAsyncSerialServer
from pymodbus.transaction import ModbusRtuFramer
from pymodbus.exceptions import ModbusException

# Device wrappers and misc local libraries
import config
import grugbus
from grugbus.devices import Eastron_SDM120, Acrel_1_Phase
import pv.meters

from pv.mqtt_wrapper import MQTTWrapper, MQTTSetting, MQTTVariable
import pv.reload, pv.controller_coroutines
import pv.solis_s5_eh1p, pv.meters

from misc import *


"""
    python3.11
    pymodbus 3.7.x

    This is a fake smartmeter, which will respond to Modbus-RTU queries on a serial port.
    It listens on a unix socket, and expects to receive the fake smartmeter data it will forward to the inverter.

    Problem:

    Previously, the fake meter was inside the main PV controller python code. Convenient for
    data sharing, but when stopping the main PV controller to load new code, it also stops the fake meter,
    which puts the inverter in safe mode, which triggers an error.

    With the fake meter in a separate process, it can keep responding even while the main code restarts,
    which makes operations much smoother.

"""

logging.basicConfig( encoding='utf-8', 
                     level=logging.INFO,
                     format='[%(asctime)s] %(levelname)s:%(message)s',
                     handlers=[
                            logging.handlers.RotatingFileHandler(Path(__file__).stem+'.log', mode='a', maxBytes=5*1024*1024, backupCount=2, encoding=None, delay=False),
                            logging.FileHandler(filename=Path(__file__).stem+'.log'), 
                            logging.StreamHandler(stream=sys.stdout)
                    ])
log = logging.getLogger(__name__)

###########################################################################################
#
#       Fake smartmeter
#       Modbus server emulating a fake smartmeter to feed data to inverter via meter port
#
###########################################################################################
#
#   Modbus server is a slave (client is master)
#   pymodbus requires ModbusSlaveContext which contains data to serve
#   This ModbusSlaveContext has a hook to generate values on the fly when requested
#
class HookModbusSlaveContext(ModbusSlaveContext):
    def getValues(self, fc_as_hex, address, count=1):
        if self._on_getValues( fc_as_hex, address, count, self ):
            return super().getValues( fc_as_hex, address, count )

###########################################################################################
#   
#   Fake smartmeter emulator
#
#   Base class is grugbus.LocalServer which extends pymodbus server class to allow
#   registers to be accessed by name with full type conversion, instead of just
#   address and raw data.
#   
###########################################################################################
#   Meter setting on Solis: "Acrel 1 Phase" ; reads fcode 3 addr 0 count 65
#   Note the Modbus manual for Acrel ACR10H corresponds to the wrong version of the meter.
#   Register map in Acrel_1_Phase.py was reverse engineered from inverter requests.
#   Solis firmware 3D0037: This meter is queried every second, Eastron is queried every two seconds.
#   Solis firmware 4A004C: All meters queried several times/s so it doesn't matter.
#
#   Note: for Solis inverters, the modbus address of the meter and the inverter are the same
#   although they are on two different buses. So if you set address to 2 in the inverter GUI, 
#   it will respond to that address on the COM port, and it will query the meter with that
#   address on the meter port.
class FakeSmartmeter( grugbus.LocalServer ):
    #
    #   port    serial port name
    #   key     machine readable name for logging, like "fake_meter_1", 
    #   name    human readable name like "Fake SDM120 for Inverter 1"
    #
    def __init__( self, port, key, name, modbus_address, meter_type, meter_placement="grid", baudrate=9600, mqtt=None, mqtt_topic=None ):
        # Create datastore corresponding to registers available in Eastron SDM120 smartmeter
        data_store = ModbusSequentialDataBlock( 0, [0]*750 )   
        slave_ctx = HookModbusSlaveContext(
            zero_mode = True,   # addresses start at zero
            di = ModbusSequentialDataBlock( 0, [0] ), # Discrete Inputs  (not used, so just one zero register)
            co = ModbusSequentialDataBlock( 0, [0] ), # Coils            (not used, so just one zero register)
            hr = data_store, # Holding Registers, we will write fake values to this datastore
            ir = data_store  # Input Registers (use the same datastore, so we don't have to check the opcode)
            )

        # hook to update datastore when we get a request
        slave_ctx._on_getValues = self._on_getValues
        slave_ctx.modbus_address = modbus_address

        # only make registers we actually need
        keys = { "active_power","voltage","current","apparent_power","reactive_power","power_factor",
                 "frequency","import_active_energy","export_active_energy"}

        # Use grugbus for data type translation
        super().__init__( slave_ctx, modbus_address, key, name, [ reg for reg in meter_type.MakeRegisters() if reg.key in keys ])

        # Create Server context and assign previously created datastore to smartmeter_modbus_address, this means our 
        # local server will respond to requests to this address with the contents of this datastore

        self.server_ctx = ModbusServerContext( { modbus_address : slave_ctx }, single=False )

        self.port = port
        self.baudrate = baudrate
        self.meter_type = meter_type
        self.meter_placement = meter_placement

        # This event is set when the real smartmeter is read 
        self.is_online = False
        self.mqtt = mqtt
        self.mqtt_topic = mqtt_topic

        self.last_query_time = 0
        self.data_timestamp = 0
        self.error_count = 0
        self.stat_tick  = Metronome( 60 )
        self.request_count = 0
        self.lags = []

    # This is called when the inverter sends a request to this server
    def _on_getValues( self, fc_as_hex, address, count, ctx ):
        #   The main meter is read in another coroutine, which also sets registers in this object. Check this was done.
        t = time.monotonic()
        self.request_count += 1

        # Print message if inverter talks to us
        if self.last_query_time < t-10.0:
            log.info("FakeMeter %s: receiving requests", self.key )
        self.last_query_time = t

        # Publish statistics
        age = t - self.data_timestamp
        self.lags.append( age )
        if (elapsed := self.stat_tick.ticked()) and self.mqtt_topic:
            self.mqtt.publish_value( self.mqtt_topic + "req_per_s", self.request_count/max(elapsed,1) )
            self.request_count = 0
        self.mqtt.publish_value( self.mqtt_topic + "lag", age, lambda x:round(x,2) )

        # Do not serve stale data
        if self.is_online:
            if age > config.FAKE_METER_MAX_AGE_IGNORE:
                self.error_count += 1
                log.error( "FakeMeter %s: data is too old (%f seconds) [%d errors]", self.key, age, self.error_count )
                self.active_power.value = 0 # Prevent runaway inverter output power
                if age > config.FAKE_METER_MAX_AGE_ABORT:
                    log.error( "FakeMeter %s: shutting down", self.key )
                    self.is_online = False

        # call reloadable routine to tweak values if necessary
        return self.is_online and pv.controller_coroutines.fakemeter_on_getvalues( self )

        # If return value is False, pymodbus server will abort the request, which the inverter
        # correctly interprets as the meter being offline

    # This function starts and runs the modbus server, and never returns as long as the server is running.
    async def start_server( self ):
        self.server = await StartAsyncSerialServer( context=self.server_ctx, 
            framer          = ModbusRtuFramer,
            ignore_missing_slaves = True,
            auto_reconnect = True,
            port            = self.port,
            timeout         = 0.3,      # parameters used by Solis inverter on meter port
            baudrate        = self.baudrate,
            bytesize        = 8,
            parity          = "N",
            stopbits        = 1,
            strict = False,
            )
        log.info("%s: exit StartAsyncSerialServer()", self.key )


###########################################################################################
#   
#   Fake smartmeter emulator
#
#   Base class is grugbus.LocalServer which extends pymodbus server class to allow
#   registers to be accessed by name with full type conversion, instead of just
#   address and raw data.
#   
###########################################################################################

class Controller:
    def __init__( self ):
        self.event_power = asyncio.Event()

        self.meter_power_tweaked      = 0    
        self.house_power              = 0    
        self.total_pv_power           = 0    # Total PV production reported by inverters. Fast, but inaccurate.
        self.total_input_power        = 0    # Power going into the inverter/battery (PV and grid port). Fast proxy for battery charging power. For routing.
        self.total_grid_port_power    = 0    # Sum of inverters local smartmeter power (negative=export)
        self.total_battery_power      = 0    # Battery power for both inverters (positive for charging)
        self.battery_max_charge_power = 0    


    def start( self ):
        with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
            runner.run(self.astart())

    async def astart( self ):    
        self.error_tick = Metronome( 10 )

        self.mqtt = MQTTWrapper( "pv_controller" )
        self.mqtt_topic = "pv/"
        await self.mqtt.mqtt.connect( config.MQTT_BROKER_LOCAL )

        # Get battery current from BMS
        MQTTVariable( "pv/bms/current", self, "bms_current", float, None, 0 )
        MQTTVariable( "pv/bms/power",   self, "bms_power",   float, None, 0 )
        MQTTVariable( "pv/bms/soc",     self, "bms_soc",     float, None, 0 )

        #   Main smartmeter
        #
        self.meter = pv.meters.SDM630(
            AsyncModbusSerialClient( **config.METER["SERIAL"] ), 
            mqtt        = self.mqtt,
            mqtt_topic  = "pv/meter/",
            **config.METER["PARAMS"],
        )

        #
        #   Solis inverters, local meters, fake meters
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
                fake_meter = FakeSmartmeter( 
                    meter_type      = Acrel_1_Phase,
                    mqtt            = self.mqtt, 
                    mqtt_topic      = ("pv/%s/fakemeter/"%key),
                    **cfg["FAKE_METER"]
                ),
                mqtt                 = self.mqtt,
                mqtt_topic           = "pv/%s/" % key,
                **cfg["PARAMS"],
            )        
            for key, cfg in config.SOLIS.items()
        ]

        for v in self.inverters:
            setattr( self, v.key, v )

        pv.reload.add_module_to_reload( "config", self.mqtt.load_rate_limit ) # reload rate limit configuration

        #   Launch it
        #
        try:
            async with asyncio.TaskGroup() as tg:
                for v in self.inverters:
                    tg.create_task( self.log_coroutine( "%s: Read"                   %v.key, v.read_coroutine() ))
                    tg.create_task( self.log_coroutine( "%s: Read local meter"       %v.key, v.local_meter.read_coroutine() ))
                    tg.create_task( self.log_coroutine( "%s: Fakemeter Modbus server"%v.key, v.fake_meter.start_server() ))
                    tg.create_task( pv.reload.reloadable_coroutine( "Powersave: %s" % v.key, lambda: pv.controller_coroutines.inverter_powersave_coroutine, self, v ))

                tg.create_task( self.log_coroutine( "Read: main meter",          self.meter.read_coroutine() ))
                tg.create_task( self.log_coroutine( "Reload python modules",     pv.reload.reload_coroutine() ))
                tg.create_task( pv.reload.reloadable_coroutine( "Inverter fan control", lambda: pv.controller_coroutines.inverter_fan_coroutine, self ))
                tg.create_task( pv.reload.reloadable_coroutine( "Power coroutine"     , lambda: pv.controller_coroutines.power_coroutine, self ))
                tg.create_task( pv.reload.reloadable_coroutine( "Sysinfo"             , lambda: pv.controller_coroutines.sysinfo_coroutine, self ))

        except (KeyboardInterrupt, CancelledError):
            print("Terminated.")
        finally:
            with open("mqtt_stats/pv_controller.txt","w") as f:
                self.mqtt.write_stats( f )
            await self.mqtt.mqtt.disconnect()

    async def log_coroutine( self, title, fut ):
        log.info("Start:"+title )
        try:        await fut
        finally:    log.info("Exit: "+title )


if 1:
    try:
        mgr = Controller()
        mgr.start()
    finally:
        logging.shutdown()
else:
    import cProfile
    with cProfile.Profile( time.process_time ) as pr:
        pr.enable()
        try:
            mgr = Controller()
            mgr.start()
        finally:
            logging.shutdown()
            pr.dump_stats("profile.dump")


















