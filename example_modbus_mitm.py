#!/usr/bin/python
# -*- coding: utf-8 -*-

import os, pprint, time, sys, serial, socket, traceback, struct, datetime, logging, math, traceback, collections

# Modbus stuff
import pymodbus, asyncio, signal, uvloop
from path import Path
from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient
from pymodbus.datastore import ModbusServerContext, ModbusSlaveContext, ModbusSequentialDataBlock
from pymodbus.server import StartAsyncSerialServer
from pymodbus.transaction import ModbusRtuFramer

# import aiohttp
# from gmqtt import Client as MQTTClient

# Device wrappers
import grugbus
from misc import *
from grugbus.devices import Eastron_SDM120, Solis_S5_EH1P_6K_2020_Extras, Eastron_SDM630, Acrel_1_Phase
import config

# pymodbus.pymodbus_apply_logging_config( logging.DEBUG )
logging.basicConfig( encoding='utf-8', 
                     level=logging.INFO,
                     format='[%(asctime)s] %(levelname)s:%(message)s',
                     handlers=[logging.FileHandler(filename=Path(__file__).stem+'.log'), 
                            logging.StreamHandler(stream=sys.stdout)])
log = logging.getLogger(__name__)

# set max reconnect wait time for Fronius
pymodbus.constants.Defaults.ReconnectDelayMax = 60000   # in milliseconds

"""

    EXAMPLE MITM SCRIPT

    This simplified script creates a fake Acrel ACR10R smartmeter as modbus server.
    It queries data from a real Acrel ACR10R meter and copies it to the fake one.
    See config.py for serial port names and other options.


#### WARNING ####
This file is both the example code and the manual.
==================================================

RS485 ports
- Master:       Solis inverters COM port + Meter attached to inverter
- Slave:        Inverter 1 fake meter
- Slave:        Inverter 2 fake meter
- Master:       Main smartmeter (gets its own interface cause it has to be fast, for power routing)

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

#
#   Housekeeping for async multitasking:
#   If one thread coroutine abort(), fire STOP event to allow program exit
#   and set STILL_ALIVE to False to stop all other threads.
#
STILL_ALIVE = True
STOP = asyncio.Event()
def abort():
    STOP.set()
    global STILL_ALIVE
    STILL_ALIVE = False

# Helper to abort program when the coroutine passed as parameter exits
async def abort_on_exit( awaitable ):
    await awaitable
    log.info("*** Exited: %s", awaitable)
    return abort()

###########################################################################################
#
#       Fake smartmeter
#       Modbus server emulating a fake smartmeter to feed data to inverter via meter port
#
#       https://pymodbus.readthedocs.io/en/v1.3.2/examples/asynchronous-server.html
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
#   This meter is queried every second, which allows Solis to update its output power
#   twice as fast as with Eastron meter, which is queried every two seconds.
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
    def __init__( self, port, key, name, modbus_address=1 ):
        self.port = port

        # Create slave context for our local server
        slave_ctxs = {}
        # Create datastore corresponding to registers available in Eastron SDM120 smartmeter
        data_store = ModbusSequentialDataBlock( 0, [0]*350 )   
        slave_ctx = HookModbusSlaveContext(
            zero_mode = True,   # addresses start at zero
            di = ModbusSequentialDataBlock( 0, [0] ), # Discrete Inputs  (not used, so just one zero register)
            co = ModbusSequentialDataBlock( 0, [0] ), # Coils            (not used, so just one zero register)
            hr = data_store, # Holding Registers, we will write fake values to this datastore
            ir = data_store  # Input Registers (use the same datastore, so we don't have to check the opcode)
            )
        slave_ctx._on_getValues = self._on_getValues # hook to update datastore when we get a request
        slave_ctx.modbus_address = modbus_address
        slave_ctxs[modbus_address] = slave_ctx

        # Create Server context and assign previously created datastore to smartmeter_modbus_address, this means our 
        # local server will respond to requests to this address with the contents of this datastore
        self.server_ctx = ModbusServerContext( slave_ctxs, single=False )
        super().__init__( slave_ctxs[1],   # dummy parameter, address is not actually used
              1, key, name, 
            Acrel_1_Phase.MakeRegisters() ) # build our registers
            # Eastron_SDM120.MakeRegisters() ) # build our registers
        self.last_request_time = time.time()    # for stats
        self.data_request_timestamp = 0
        self.power_offset = 0
        self.power_elimit = 2000
        self.export_mode = 0

    # This is called when the inverter sends a request to this server
    def _on_getValues( self, fc_as_hex, address, count, ctx ):

        #   The main meter is read in another coroutine, so data is already available and up to date
        #   but we still have to check if the meter is actually working
        #
        #   TODO: get data from your data source and check if it is online
        meter = mgr.meter
        if not meter.is_online:
            log.warning( "FakeSmartmeter cannot reply to client: real smartmeter offline" )
            # return value is False so pymodbus server will abort the request, which the inverter
            # correctly interprets as the meter being offline
            return False

        # TODO: fill this with your data
        # Solis uses active_power for control loop, the others are for display only (I think)
        try:    # Fill our registers with up-to-date data
            self.voltage                .value = meter.voltage              .value
            self.current                .value = meter.current              .value
            self.apparent_power         .value = meter.apparent_power       .value
            self.reactive_power         .value = meter.reactive_power       .value
            self.power_factor           .value =(meter.power_factor         .value % 1.0)
            self.frequency              .value = meter.frequency            .value
            self.import_active_energy   .value = meter.import_active_energy .value
            self.export_active_energy   .value = meter.export_active_energy .value
            self.active_power           .value = meter.active_power         .value

        except TypeError:   # if one of the registers was None because it wasn't read yet
            log.warning( "FakeSmartmeter cannot reply to client: real smartmeter missing fields" )
            return

        self.write_regs_to_context() # write data to modbus server context, so it can be served to inverter

        # logging
        # t = time.time()
        # # s = "query _on_getValues fc %3d addr %5d count %3d dt %f" % (fc_as_hex, address, count, t-self.last_request_time)
        # # log.debug(s); 
        # self.last_request_time = t
        # if meter.data_timestamp:    # how fresh is this data?
        #     mqtt.publish( "pv/solis1/fakemeter/", {
        #         "lag": round( t-meter.data_timestamp, 2 ), # lag between getting data from the real meter and forwarding it to the inverter
        #         self.active_power.key: self.active_power.format_value(), # log what we sent to the inverter
        #         "offset": int(self.power_offset)
        #         })
        return True

    # This function starts and runs the modbus server, and never returns as long as the server is running.
    # Before starting it, communication with the real meter should be initiated, registers read,
    # dummy registers in this object populated with correct values, and write_regs_to_context() called
    # to setup the server context, so that we serve correct value to the inverter when it makes a request.
    async def start_server( self ):
        self.server = await StartAsyncSerialServer( context=self.server_ctx, 
            framer          = ModbusRtuFramer,
            ignore_missing_slaves = True,
            auto_reconnect = True,
            port            = self.port,
            timeout         = 0.3,      # parameters used by Solis inverter on meter port
            baudrate        = 9600,
            bytesize        = 8,
            parity          = "N",
            stopbits        = 1,
            strict = False,
            # response_manipulator = self.response_manipulator
            )
        await self.server.start()

########################################################################################
#
#       Main house smartmeter, grid side, meters total power for solar+home
#
########################################################################################
class MainSmartmeter( grugbus.SlaveDevice ):
    def __init__( self ):
        super().__init__( 
            AsyncModbusSerialClient(
                port            = config.COM_PORT_METER,
                timeout         = 0.3,
                retries         = config.MODBUS_RETRIES_SOLIS,
                retry_on_empty  = True,
                baudrate        = 9600,
                bytesize        = 8,
                parity          = "N",
                stopbits        = 1,
                strict = False
                # framer=pymodbus.ModbusRtuFramer,
            ),
            1,          # Modbus address
            "meter", "Acrel_1_Phase", 
            Acrel_1_Phase.MakeRegisters() )
        self.is_online = False

    async def read_coroutine( self ):
        regs_to_read = (
            self.voltage                ,
            self.current                ,
            self.apparent_power         ,
            self.reactive_power         ,
            self.power_factor           ,
            self.frequency              ,
            self.import_active_energy   ,
            self.export_active_energy   ,
            self.active_power           ,
        )

        tick = Metronome(config.POLL_PERIOD_METER)
        while STILL_ALIVE:
            try:
                if not self.modbus.connected:
                    await self.modbus.connect()

                try:
                    regs = await self.read_regs( regs_to_read )
                    self.is_online = True
                except asyncio.exceptions.TimeoutError:
                    self.is_online = False
                except:
                    self.is_online = False
                    raise
            except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
                return abort()
            except:
                s = traceback.format_exc()
                log.error(s)
                await asyncio.sleep(0.5)
            await tick.wait()


########################################################################################
#
#       Put it all together
#
########################################################################################
class SolisManager():
    def __init__( self ):

        self.meter = MainSmartmeter()
        self.fake_meter = FakeSmartmeter( config.COM_PORT_FAKE_METER1, "fake_meter_1", "Fake SDM120 for Inverter 1" )

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
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGINT,  abort)
        loop.add_signal_handler(signal.SIGTERM, abort)

        asyncio.create_task( self.fake_meter.start_server() )
        asyncio.create_task( abort_on_exit( self.meter.read_coroutine() ))
        asyncio.create_task( self.display_coroutine() )

        await STOP.wait()

    ########################################################################################
    #   Local display
    ########################################################################################

    async def display_coroutine( self ):
        while STILL_ALIVE:
            await asyncio.sleep(1)
            r = [""]

            try:
                for reg in (
                    self.meter.voltage             ,
                    self.meter.current             ,
                    self.meter.apparent_power      ,
                    self.meter.reactive_power      ,
                    self.meter.power_factor        ,
                    self.meter.frequency           ,
                    self.meter.import_active_energy,
                    self.meter.export_active_energy,
                    self.meter.active_power        ,                    
                    ):
                    if isinstance( reg, str ):
                        r.append(reg)
                    else:
                        if reg.value != None:
                            r.append( "%40s %10s %10s" % (reg.key, reg.device.key, reg.format_value() ) )

                # print( "\n".join(r) )
            except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
                return abort()
            except:
                log.error(traceback.format_exc())



mgr = SolisManager()
try:
    mgr.start()
finally:
    logging.shutdown()

