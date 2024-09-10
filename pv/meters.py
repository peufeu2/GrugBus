#!/usr/bin/python
# -*- coding: utf-8 -*-

import time, asyncio, datetime, logging, collections, traceback, pymodbus
from pymodbus.exceptions import ModbusException
from asyncio.exceptions import TimeoutError

from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient
from pymodbus.datastore import ModbusServerContext, ModbusSlaveContext, ModbusSequentialDataBlock
from pymodbus.server import StartAsyncSerialServer
from pymodbus.transaction import ModbusRtuFramer

# Device wrappers and misc local libraries
import grugbus
from grugbus.devices import Eastron_SDM630, Eastron_SDM120
import config
from misc import *

log = logging.getLogger(__name__)


########################################################################################
#
#       Main house smartmeter, grid side, meters total power for solar+home
#
#       This class reads the meter and publishes it on MQTT
#
#       Meter is read very often, so it gets its own serial port
#
########################################################################################
class SDM630( grugbus.SlaveDevice ):
    def __init__( self, modbus, modbus_addr, key, name, mqtt, mqtt_topic, mgr ):
        super().__init__( modbus, modbus_addr, key, name, Eastron_SDM630.MakeRegisters() ),
        self.mqtt        = mqtt
        self.mqtt_topic  = mqtt_topic
        self.mgr = mgr

        self.total_power_tweaked = 0.0
        self.event_power = asyncio.Event()  # Fires every time frequent_regs below are read
        self.event_all   = asyncio.Event()  # Fires when all registers are read, for slower processes
        self.tick = Metronome(config.POLL_PERIOD_METER)  # fires a tick on every period to read periodically, see misc.py

        # For power routing to work we need to read total_power frequently. So we don't read 
        # ALL registers every time. Instead, gather the unimportant ones in little groups
        # and frequently read THE important register (total_power) + one group.
        # Unimportant registers will be updated less often, who cares.
        self.reg_sets = ((
            self.total_power                      ,    # required for fakemeter
            self.total_volt_amps                  ,    # required for fakemeter
            self.total_var                        ,    # required for fakemeter
            self.total_power_factor               ,    # required for fakemeter
            self.total_phase_angle                ,    # required for fakemeter
            self.frequency                        ,    # required for fakemeter
            self.total_import_kwh                 ,    # required for fakemeter
            self.total_export_kwh                 ,    # required for fakemeter
            self.total_import_kvarh               ,    # required for fakemeter
            self.total_export_kvarh               ,    # required for fakemeter
        ),(
            self.total_power                      ,    # required for fakemeter
            self.phase_1_line_to_neutral_volts    ,    # required for fakemeter
            self.phase_2_line_to_neutral_volts    ,
            self.phase_3_line_to_neutral_volts    ,
            self.phase_1_current                  ,    # required for fakemeter
            self.phase_2_current                  ,
            self.phase_3_current                  ,
            self.phase_1_power                    ,
            self.phase_2_power                    ,
            self.phase_3_power                    ,
        ),(
            self.total_power                      ,    # required for fakemeter
            self.total_kwh                        ,    # required for fakemeter
            self.total_kvarh                      ,    # required for fakemeter
        ),(
            self.total_power                      ,    # required for fakemeter
            self.average_line_to_neutral_volts_thd,
            self.average_line_current_thd         ,
        ))

        # publish these to MQTT
        self.regs_to_publish = set((
            self.phase_1_line_to_neutral_volts    ,
            self.phase_2_line_to_neutral_volts    ,
            self.phase_3_line_to_neutral_volts    ,
            self.phase_1_current                  ,
            self.phase_2_current                  ,
            self.phase_3_current                  ,
            self.phase_1_power                    ,
            self.phase_2_power                    ,
            self.phase_3_power                    ,
            self.total_power                      ,
            self.total_import_kwh                 ,
            self.total_export_kwh                 ,
            self.total_volt_amps                  ,
            self.total_var                        ,
            self.total_power_factor               ,
            # self.total_phase_angle                ,
            self.average_line_to_neutral_volts_thd,
            self.average_line_current_thd         ,
                ))

    async def read_coroutine( self ):
        last_poll_time = None
        mqtt  = self.mqtt
        topic = self.mqtt_topic
        while True:
            for reg_set in self.reg_sets:
                try:
                    await self.tick.wait()
                    try:
                        regs = await self.read_regs( reg_set )
                    finally:
                        # wake up other coroutines waiting for fresh values
                        # even if there was a timeout
                        self.event_power.set()
                        self.event_power.clear()

                    for reg in self.regs_to_publish.intersection(regs):
                         mqtt.publish_reg( topic, reg )

                    mqtt.publish_value( topic+"is_online", int( self.is_online ))   # set by read_regs(), True if it succeeded, False otherwise

                    if config.LOG_MODBUS_REQUEST_TIME:
                        mqtt.publish_value( topic+"req_time", round( self.last_transaction_duration,2 ))   # log modbus request time, round it for better compression
                        if last_poll_time:
                            mqtt.publish_value( topic+"req_period", self.last_transaction_timestamp - last_poll_time )
                        last_poll_time = self.last_transaction_timestamp

                except (TimeoutError, ModbusException):
                    await asyncio.sleep(1)

                except Exception:
                    self.is_online = False
                    log.exception(self.key+":")
                    await asyncio.sleep(0.5)

            # wake up other coroutines waiting for fresh values
            self.event_all.set()
            self.event_all.clear()


########################################################################################
#
#       Local smartmeter on inverter grid port (not the global one for the house)
#
########################################################################################
class SDM120( grugbus.SlaveDevice ):
    def __init__( self, modbus, modbus_addr, key, name, mqtt, mqtt_topic ):
        super().__init__( modbus, modbus_addr, key, name, Eastron_SDM120.MakeRegisters() ),
        self.mqtt        = mqtt
        self.mqtt_topic  = mqtt_topic

        #   Other coroutines that need register values can wait on these events to 
        #   grab the values when they are read
        self.event_power = asyncio.Event()  # Fires every time frequent_regs below are read
        self.event_all   = asyncio.Event()  # Fires when all registers are read, for slower processes
        self.tick = Metronome( config.POLL_PERIOD_SOLIS_METER )
        self.reg_sets = [[self.active_power]] * 9 + [[
            self.active_power          ,
            # self.apparent_power        ,
            # self.reactive_power        ,
            # self.power_factor          ,
            # self.phase_angle           ,
            # self.frequency             ,
            self.import_active_energy  ,
            self.export_active_energy  ,
            # self.import_reactive_energy,
            # self.export_reactive_energy,
            # self.total_active_energy   ,
            # self.total_reactive_energy ,
        ]]

    async def read_coroutine( self ):
        mqtt  = self.mqtt
        topic = self.mqtt_topic

        while True:
            for reg_set in self.reg_sets:
                try:
                    await self.tick.wait()
                    try:
                        regs = await self.read_regs( reg_set )
                    finally:
                        # wake up other coroutines waiting for fresh values
                        self.event_power.set()
                        self.event_power.clear()

                    for reg in regs:
                        mqtt.publish_reg( topic, reg )

                except (TimeoutError, ModbusException):
                    await asyncio.sleep(1)

                except Exception:
                    self.is_online = False
                    log.exception(self.key+":")
                    # s = traceback.format_exc()
                    # log.error(self.key+":"+s)
                    # self.mqtt.mqtt.publish( "pv/exception", s )
                    await asyncio.sleep(1)

            # wake up other coroutines waiting for fresh values
            self.event_all.set()
            self.event_all.clear()


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
    def __init__( self, port, key, name, modbus_address, meter_type, meter_placement="grid", mqtt=None ):
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
        self.meter_type = meter_type
        self.meter_placement = meter_placement

        # This event is set when the real smartmeter is read 
        self.is_online = False
        self.mqtt = mqtt

    # This is called when the inverter sends a request to this server
    def _on_getValues( self, fc_as_hex, address, count, ctx ):
        #   The main meter is read in another coroutine, which also sets registers in this object. Check this was done.
        if not self.is_online:
            # return value is False so pymodbus server will abort the request, which the inverter
            # correctly interprets as the meter being offline
            # log.warning( "FakeSmartmeter cannot reply to client: real smartmeter offline" )
            return False

        # if enabled, publish lag time
        if self.mqtt and self.data_timestamp:    # how fresh is this data?
            self.mqtt.publish_value( "pv/solis1/fakemeter/lag", round( time.monotonic()-self.data_timestamp, 2 ) )

        # tell pymodbus to serve the request
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
            )
        # await self.server.start()     # this was for an old pymodbus version


