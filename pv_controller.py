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

        # Use grugbus for data type translation
        super().__init__( slave_ctx, modbus_address, key, name, meter_type.MakeRegisters() )

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
        self.error_tick = Metronome( 10 )
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
            if age > 2:
                self.error_count += 1
                if self.error_tick.ticked():
                    log.error( "FakeMeter %s: data is too old (%f seconds) [%d errors]", self.key, age, self.error_count )
                    self.active_power.value = 0 # Prevent runaway inverter output power
                if age > 10.0:
                    log.error( "FakeMeter %s: shutting down", self.key )
                    self.is_online = False

        # print( self.is_online, self.active_power.value )

        return self.is_online
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
        pass

    def start( self ):
        with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
            runner.run(self.astart())

    async def astart( self ):    
        self.error_tick = Metronome( 10 )

        self.mqtt = MQTTWrapper( "pv_controller" )
        self.mqtt_topic = "pv/"
        await self.mqtt.mqtt.connect( config.MQTT_BROKER_LOCAL )

        #   Update fakemeters via MQTT
        #
        MQTTVariable( "nolog/pv/fakemeter_update", self, "fakemeter_data", orjson.loads, None, "{}", self.mqtt_update_meter_callback )

        #   Main smartmeter
        #
        self.meter = pv.meters.SDM630(
            AsyncModbusSerialClient( **config.METER["SERIAL"] ), 
            mqtt        = self.mqtt,
            mqtt_topic  = "pv/meter/",
            **config.METER["PARAMS"],
        )

        #   Modbus-RTU Fake Meter
        #
        self.fake_meters = {
            key: FakeSmartmeter( 
                    meter_type      = Acrel_1_Phase,
                    mqtt            = self.mqtt, 
                    mqtt_topic      = ("pv/%s/fakemeter/"%key),
                    **cfg["FAKE_METER"]
                )
            for key, cfg in config.SOLIS.items()
        }

        #   Launch it
        #
        try:
            async with asyncio.TaskGroup() as tg:
                for k, v in self.fake_meters.items():
                    tg.create_task( self.log_coroutine( "Fakemeter: Modbus server %s"%k, v.start_server() ))
                tg.create_task( self.log_coroutine( "Read: main meter",          self.meter.read_coroutine() ))
                tg.create_task( self.log_coroutine( "Fakemeter: update fields",  self.update_coroutine( ) ))
                tg.create_task( self.log_coroutine( "sysinfo",                   self.sysinfo_coroutine() ))
                tg.create_task( self.log_coroutine( "Reload python modules",     self.reload_coroutine() ))
        except (KeyboardInterrupt, CancelledError):
            print("Terminated.")
        finally:
            with open("mqtt_stats/pv_controller.txt","w") as f:
                self.mqtt.write_stats( f )
            await self.mqtt.mqtt.disconnect()

    ########################################################################################
    #
    #   Compute power values and fill fake meter fields
    #
    ########################################################################################
    async def mqtt_update_meter_callback( self, param ):
        if not self.fakemeter_data.value:
            if self.error_tick.ticked():
                log.warning( "FakeMeters: No data from master." )
            return

        t = time.monotonic()
        ts = self.fakemeter_data.value["data_timestamp"]
        age = t-ts
        if age > config.FAKE_METER_MAX_AGE:
            self.fakemeter_data.value = {}
            log.warning( "FakeMeter: data is too old (%f seconds), ignoring", age )
            return

        if not self.fakemeter_data.prev_value:
            log.info( "FakeMeter: received data from master (age %f seconds)", age )

        for k, fm in self.fake_meters.items():
            data = self.fakemeter_data.value[k]
            fm.active_power           .value = data[ "active_power" ]
            fm.data_timestamp                = ts

        # if there was no exception, we can update both meters
        for fm in self.fake_meters.values():
            fm.write_regs_to_context()

    async def update_coroutine( self ):
        m = self.meter
        await m.event_all.wait()    # wait for all registers to be read

        while True:
            try:
                await m.event_power.wait()

                # Pre-fill all the fake meter fields with defaults. If data received from master is good, we will overwrite below.
                # Divide power between all inverters, without checking if they're online or not. Only the master process knows that.
                fake_power              = (m.total_power.value or 0) / len( self.fake_meters )

                # Fill all fields with defaults
                for k, fm in self.fake_meters.items():
                    fm.active_power           .value = fake_power
                    fm.voltage                .value = m.phase_1_line_to_neutral_volts .value
                    fm.current                .value = m.phase_1_current               .value
                    fm.apparent_power         .value = m.total_volt_amps               .value
                    fm.reactive_power         .value = m.total_var                     .value
                    fm.power_factor           .value = (m.total_power_factor            .value % 1.0)
                    fm.frequency              .value = m.frequency                     .value
                    fm.import_active_energy   .value = m.total_import_kwh              .value
                    fm.export_active_energy   .value = m.total_export_kwh              .value
                    fm.data_timestamp                = m.last_transaction_timestamp
                    fm.is_online                     = m.is_online

                # Now update with values received from master (if any)
                try:
                    await self.mqtt_update_meter_callback( None )
                except Exception:
                    log.exception("PowerManager coroutine:")

                for fm in self.fake_meters.values():
                    fm.write_regs_to_context()
                    self.mqtt.publish_reg( fm.mqtt_topic, fm.active_power )

            except Exception:
                log.exception("PowerManager coroutine:")

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
                for k,v in pub.items():
                    self.mqtt.publish_value( "pv/"+k, v  )

            except Exception:
                log.exception( "Sysinfo" )

            await asyncio.sleep(10)


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


















