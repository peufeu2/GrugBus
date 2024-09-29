#!/usr/bin/python
# -*- coding: utf-8 -*-

import sys, time, serial, socket, logging, traceback, shutil, uvloop, asyncio, orjson
from path import Path
from asyncio.exceptions import TimeoutError, CancelledError

# Modbus
import pymodbus
from pymodbus.datastore import ModbusServerContext, ModbusSlaveContext, ModbusSequentialDataBlock
from pymodbus.server import StartAsyncSerialServer
from pymodbus.transaction import ModbusRtuFramer
from pymodbus.exceptions import ModbusException

# Device wrappers and misc local libraries
import config
import grugbus
from grugbus.devices import Acrel_1_Phase
from pv.mqtt_wrapper import MQTTWrapper
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
                            # logging.handlers.RotatingFileHandler(Path(__file__).stem+'.log', mode='a', maxBytes=5*1024*1024, backupCount=2, encoding=None, delay=False),
                            logging.FileHandler(filename=Path(__file__).stem+'.log'), 
                            logging.StreamHandler(stream=sys.stdout)
                    ])
log = logging.getLogger(__name__)

async def log_coroutine( title, fut ):
    log.info("Start:"+title )
    try:        await fut
    finally:    log.info("Exit: "+title )

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
        self.mqtt.publish_value( self.mqtt_topic + "lag", age )

        # Do not serve stale data
        if self.is_online:
            if age > 1.5:
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


class FakeMeterServer:
    def __init__( self ):
        self.mqtt            = MQTTWrapper( "pv_fake_meter" )

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
        #   Modbus-RTU Fake Meter
        #
        self.fake_meters = {
            "solis1":  FakeSmartmeter( 
                    port            = config.COM_PORT_FAKE_METER1, 
                    baudrate        = 9600, 
                    key             = "fake_meter_1", 
                    name            = "Fake meter for Solis 1", 
                    modbus_address  = 1, 
                    meter_type      = Acrel_1_Phase,
                    mqtt            = self.mqtt, 
                    mqtt_topic      = "pv/solis1/fakemeter/"
                ),
            "solis2":  FakeSmartmeter( 
                    port            = config.COM_PORT_FAKE_METER2, 
                    baudrate        = 9600, 
                    key             = "fake_meter_2", 
                    name            = "Fake meter for Solis 2", 
                    modbus_address  = 1, 
                    meter_type      = Acrel_1_Phase,
                    mqtt            = self.mqtt, 
                    mqtt_topic      = "pv/solis2/fakemeter/"
                )
        }
 
        #   Unix socket server
        #
        self.socket_server = await asyncio.start_unix_server( self.handle_client, config.SOCKET_FAKE_METER )

        await self.mqtt.mqtt.connect( config.MQTT_BROKER_LOCAL )

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task( log_coroutine( "Socket server", self.socket_server.serve_forever() ))
                tg.create_task( log_coroutine( "Modbus server", self.fake_meters["solis1"].start_server() ))
                tg.create_task( log_coroutine( "Modbus server", self.fake_meters["solis2"].start_server() ))
        except (KeyboardInterrupt, CancelledError):
            print("Terminated.")
        finally:
            await self.mqtt.mqtt.disconnect()
            with open("mqtt_stats/fake_meter_server.txt","w") as f:
                self.mqtt.write_stats( f )

    ########################################################
    #   Server
    ########################################################
    async def handle_client( self, reader, writer ):
        try:
            log.info("Client connected")
            async for line in reader:
                received = orjson.loads( line )

                for key, data in received.items():
                    fm = self.fake_meters[key]
                    fm.active_power           .value = data[ "active_power" ]
                    fm.voltage                .value = data[ "voltage" ]
                    fm.current                .value = data[ "current" ]
                    fm.apparent_power         .value = data[ "apparent_power" ]
                    fm.reactive_power         .value = data[ "reactive_power" ]
                    fm.power_factor           .value = data[ "power_factor" ]
                    fm.frequency              .value = data[ "frequency" ]
                    fm.import_active_energy   .value = data[ "import_active_energy" ]
                    fm.export_active_energy   .value = data[ "export_active_energy" ]
                    fm.data_timestamp                = data[ "data_timestamp" ]
                    fm.is_online                     = data[ "is_online" ]
                    fm.write_regs_to_context()
                    self.mqtt.publish_reg( fm.mqtt_topic, fm.active_power )

        except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
            raise
        except:
            log.exception('Exception')
            raise
        finally:
            log.info("Client disconnected")

if 1:
    try:
        mgr = FakeMeterServer( )
        mgr.start()
    finally:
        pass
else:
    import cProfile
    with cProfile.Profile( time.process_time ) as pr:
        pr.enable()
        try:
            mgr = FakeMeterServer( )
            mgr.start()
        finally:
            pr.dump_stats("fake_meter_profile.dump")







