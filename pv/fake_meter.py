#!/usr/bin/python
# -*- coding: utf-8 -*-

import os, pprint, time, asyncio

# Modbus
from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient
from pymodbus.datastore import ModbusServerContext, ModbusSlaveContext, ModbusSequentialDataBlock
from pymodbus.server import StartAsyncSerialServer
from pymodbus.transaction import ModbusRtuFramer

# Device wrappers and misc local libraries
import grugbus
import config
from misc import *

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
            self.mqtt.publish( "pv/solis1/fakemeter/", { "lag": round( time.monotonic()-self.data_timestamp, 2 ) } )

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


