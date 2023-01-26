#!/usr/bin/python
# -*- coding: utf-8 -*-

import os, pprint, time, sys, serial, socket, traceback, struct, datetime, logging, math, traceback

# Modbus stuff
import pymodbus, asyncio, signal
# import uvloop
from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient
from pymodbus.datastore import ModbusServerContext, ModbusSlaveContext, ModbusSequentialDataBlock
from pymodbus.server import StartAsyncSerialServer
from pymodbus.transaction import ModbusRtuFramer

from gmqtt import Client as MQTTClient

# Device wrappers
import grugbus
from grugbus.devices import Eastron_SDM120, Solis_S5_EH1P_6K_2020_Extras, Eastron_SDM630, Acrel_1_Phase
import config

# pymodbus.pymodbus_apply_logging_config( logging.DEBUG )
logging.basicConfig( encoding='utf-8', 
                     level=logging.INFO,
                     format='[%(asctime)s] %(levelname)s:%(message)s',
                     handlers=[logging.FileHandler(filename='modbus_mitm.log'), 
                            logging.StreamHandler(stream=sys.stdout)])
log = logging.getLogger(__name__)

# set max reconnect wait time for Fronius
pymodbus.constants.Defaults.ReconnectDelayMax = 60000   # in milliseconds

"""
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
git remote set-url origin https://<token>@github.com/peufeu2/pymodbus.git 

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
#   Helper to set modbus address of a SDM120 smartmeter during installation:
#       
#       It has default address 1, so use this function to change it.
#
if 0:
    async def set_sdm120_address( new_address=4 ):
        d = grugbus.SlaveDevice( 
                AsyncModbusSerialClient(
                    port            = "COM7",
                    timeout         = 0.3,
                    retries         = 2,
                    retry_on_empty  = True,
                    baudrate        = 9600,
                    bytesize        = 8,
                    parity          = "N",
                    stopbits        = 1,
                    strict = False
                    # framer=pymodbus.ModbusRtuFramer,
                ),
                1,          # Modbus address
                "meter", "SDM630 Smartmeter", 
                Eastron_SDM120.MakeRegisters() )
        await d.modbus.connect()

        print("Checking meter on address %s" % d.bus_address )
        await d.rwr_modbus_node_address.read()
        print( "current address", d.rwr_modbus_node_address.value )

        input( "Long press button on meter until display --SET-- to enable modbus writes (otherwise it is protected) and press ENTER" )
        d.rwr_modbus_node_address.value = new_address
        await d.rwr_modbus_node_address.write()

        print( "Write done, if it was successful the meter should not respond to the old address and this should raise a Timeout...")

        await d.rwr_modbus_node_address.read()
        print( "current address", d.rwr_modbus_node_address.value )

    asyncio.run( set_sdm120_address() )
    stop

#
#   Blink LEDs on RS485 dongles in turn so I know which is which
#
# for name in "COM_PORT_SOLIS","COM_PORT_FAKE_METER", "COM_PORT_METER":
#     print(name)
#     with serial.Serial( port=globals()[name] ) as ser:
#         ser.write( b"x"*1000 )
#     input()

#
#   Housekeeping
#
STILL_ALIVE = True
STOP = asyncio.Event()
def abort():
    STOP.set()
    global STILL_ALIVE
    STILL_ALIVE = False

# No zombie coroutines allowed
async def abort_on_exit( awaitable ):
    await awaitable
    log.info("*** Exited: %s", awaitable)
    return abort()

########################################################################################
#   MQTT
########################################################################################
class MQTT():
    def __init__( self ):
        self.mqtt = MQTTClient("pv")
        self.mqtt.on_connect    = self.on_connect
        self.mqtt.on_message    = self.on_message
        self.mqtt.on_disconnect = self.on_disconnect
        self.mqtt.on_subscribe  = self.on_subscribe
        self.mqtt.set_auth_credentials( config.MQTT_USER, config.MQTT_PASSWORD )
        self.published_data = {}

    def on_connect(self, client, flags, rc, properties):
        pass

    def on_disconnect(self, client, packet, exc=None):
        pass

    def on_message(self, client, topic, payload, qos, properties):
        print( "MQTT", payload )

    def on_subscribe(self, client, mid, qos, properties):
        print('MQTT SUBSCRIBED')

    def publish( self, prefix, data, add_heartbeat=False ):
        if not self.mqtt.is_connected:
            log.error( "Trying to publish %s on unconnected MQTT" % prefix )
            return

        t = time.time()
        to_publish = {}
        if add_heartbeat:
            data["heartbeat"] = int(t) # do not publish more than 1 heartbeat per second

        #   do not publish duplicate data
        for k,v in data.items():
            k = prefix+k
            p = self.published_data.get(k)
            if p:
                if p[0] == v and t<p[1]:
                    continue
            self.published_data[k] = v,t+60 # set timeout to only publish constant data every N seconds
            to_publish[k] = v

        for k,v in to_publish.items():
            self.mqtt.publish( k, str(v), qos=0 )

mqtt = MQTT()

###########################################################################################
#
#       Fake smartmeter
#       Modbus server emulating a fake smartmeter to feed data to inverter via meter port
#
###########################################################################################
#
#   ModbusSlaveContext with a hook in it to generate values on the fly when requested
#
class HookModbusSlaveContext(ModbusSlaveContext):
    def getValues(self, fc_as_hex, address, count=1):
        if self._on_getValues( fc_as_hex, address, count ):
            return super().getValues( fc_as_hex, address, count )

# #   Eastron SDM120 ; reads fcode 4 addr 342 count 2, then 4 0 76
# #   Solis does one read per second, so active_power is only updated every second
# class FakeSmartmeter( grugbus.LocalServer ):
#     def __init__( self, port, key, name, smartmeter_modbus_address=1 ):
#         self.port = port
#         # Create datastore corresponding to registers available in Eastron SDM120 smartmeter
#         self.data_store = ModbusSequentialDataBlock( 0, [0]*350 )

#         # Create slave context for our local server
#         self.slave_ctx = HookModbusSlaveContext(
#             zero_mode = True,   # addresses start at zero
#             di = ModbusSequentialDataBlock( 0, [0] ), # Discrete Inputs  (not used, so just one zero register)
#             co = ModbusSequentialDataBlock( 0, [0] ), # Coils            (not used, so just one zero register)
#             hr = self.data_store, # Holding Registers, we will write fake values to this datastore
#             ir = self.data_store  # Input Registers (use the same one, SDM120 doesn't care)
#             )
#         self.slave_ctx._on_getValues = self._on_getValues

#         # Create Server context and assign previously created datastore to address 1, this means our local server 
#         # will respond to requests to this address with the contents of this datastore
#         self.server_ctx = ModbusServerContext( { smartmeter_modbus_address: self.slave_ctx }, single=False )

#         # build our registers
#         super().__init__( self.slave_ctx, 1, key, name, Eastron_SDM120.MakeRegisters() )
#         self.last_request_time = time.time()

#         self.data_request_timestamp = 0

#     # This is called when the inverter sends a request to this server
#     # Solis S5-EH1P requests 4,342,2 and 4,0,76 in turn every second, so it gets a power update
#     # only every 2 seconds.
#     def _on_getValues( self, fc_as_hex, address, count ):

#         meter = mgr.meter
#         if not meter.is_online:
#             print( "FakeSmartmeter cannot reply to client: real smartmeter offline" )
#             return []

#         # forward these registers to the fakemeter
#         try:
#             self.voltage                .value = meter.phase_1_line_to_neutral_volts .value
#             self.current                .value = meter.phase_1_current               .value
#             self.active_power           .value = meter.total_power                   .value
#             self.apparent_power         .value = meter.total_volt_amps               .value
#             self.reactive_power         .value = meter.total_var                     .value
#             self.power_factor           .value = meter.total_power_factor            .value
#             self.phase_angle            .value = meter.total_phase_angle             .value
#             self.frequency              .value = meter.frequency                     .value
#             self.import_active_energy   .value = meter.total_import_kwh              .value
#             self.export_active_energy   .value = meter.total_export_kwh              .value
#             self.import_reactive_energy .value = meter.total_import_kvarh            .value
#             self.export_reactive_energy .value = meter.total_export_kvarh            .value
#             self.total_active_energy    .value = meter.total_kwh                     .value
#             self.total_reactive_energy  .value = meter.total_kvarh                   .value
#         except TypeError:
#             print( "FakeSmartmeter cannot reply to client: real smartmeter missing fields" )
#             return []

#         self.write_regs_to_context() # write data to modbus server context, so it can be served to inverter when it requests it.

#         t = time.time()
#         # s = "query _on_getValues fc %3d addr %5d count %3d dt %d" % (fc_as_hex, address, count, t-self.last_request_time)
#         # print(s)
#         # mqtt.mqtt.publish("pv/query", s)
#         self.last_request_time = t

#         if meter.data_timestamp:
#             mqtt.publish( "pv/solis1/fakemeter/", {
#                 "lag": round( t-meter.data_timestamp, 2 ),
#                 self.active_power.key: self.active_power.format_value()
#                 })

#         return True

#     # This function starts and runs the modbus server, and never returns as long as the server is running.
#     # Before starting it, communication with the real meter should be initiated, registers read,
#     # dummy registers in this object populated with correct values, and write_regs_to_context() called
#     # to setup the server context, so that we serve correct value to the inverter when it makes a request.
#     async def start_server( self ):
#         self.server = await StartAsyncSerialServer( context=self.server_ctx, 
#             framer          = ModbusRtuFramer,
#             ignore_missing_slaves = True,
#             auto_reconnect = True,
#             port            = self.port,
#             timeout         = 0.3,      # parameters used by Solis inverter on meter port
#             baudrate        = 9600,
#             bytesize        = 8,
#             parity          = "N",
#             stopbits        = 1,
#             strict = False,
#             # response_manipulator = self.response_manipulator
#             )
#         await self.server.start()

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
    def __init__( self, port, key, name, smartmeter_modbus_address=1 ):
        self.port = port
        self.data_store = ModbusSequentialDataBlock( 0, [0]*350 )   # Create datastore corresponding to registers available in Eastron SDM120 smartmeter

        # Create slave context for our local server
        self.slave_ctx = HookModbusSlaveContext(
            zero_mode = True,   # addresses start at zero
            di = ModbusSequentialDataBlock( 0, [0] ), # Discrete Inputs  (not used, so just one zero register)
            co = ModbusSequentialDataBlock( 0, [0] ), # Coils            (not used, so just one zero register)
            hr = self.data_store, # Holding Registers, we will write fake values to this datastore
            ir = self.data_store  # Input Registers (use the same datastore, so we don't have to check the opcode)
            )
        self.slave_ctx._on_getValues = self._on_getValues # hook

        # Create Server context and assign previously created datastore to smartmeter_modbus_address, this means our 
        # local server will respond to requests to this address with the contents of this datastore
        self.server_ctx = ModbusServerContext( { smartmeter_modbus_address: self.slave_ctx }, single=False )
        super().__init__( self.slave_ctx, 1, key, name, Acrel_1_Phase.MakeRegisters() ) # build our registers
        self.last_request_time = time.time()    # for stats
        self.data_request_timestamp = 0

    # This is called when the inverter sends a request to this server
    def _on_getValues( self, fc_as_hex, address, count ):
        meter = mgr.meter
        if not meter.is_online:
            log.warning( "FakeSmartmeter cannot reply to client: real smartmeter offline" )
            return

        try:    # Fill our registers with up-to-date data
            self.voltage                .value = meter.phase_1_line_to_neutral_volts .value
            self.current                .value = meter.phase_1_current               .value
            self.active_power           .value = meter.total_power                   .value
            self.apparent_power         .value = meter.total_volt_amps               .value
            self.reactive_power         .value = meter.total_var                     .value
            self.power_factor           .value = (meter.total_power_factor            .value) % 1.0
            self.frequency              .value = meter.frequency                     .value
            self.import_active_energy   .value = meter.total_import_kwh              .value
            self.export_active_energy   .value = meter.total_export_kwh              .value
        except TypeError:   # if one of the registers was None because it wasn't read yet
            log.warning( "FakeSmartmeter cannot reply to client: real smartmeter missing fields" )
            return

        self.write_regs_to_context() # write data to modbus server context, so it can be served to inverter when it requests it.

        t = time.time()
        # s = "query _on_getValues fc %3d addr %5d count %3d dt %f" % (fc_as_hex, address, count, t-self.last_request_time)
        # log.debug(s); 
        # mqtt.mqtt.publish("pv/query", s)
        self.last_request_time = t
        if meter.data_timestamp:    # how fresh is this data?
            mqtt.publish( "pv/solis1/fakemeter/", {
                "lag": round( t-meter.data_timestamp, 2 ), # lag between getting data from the real meter and forwarding it to the inverter
                self.active_power.key: self.active_power.format_value() # log what we sent to the inverter
                })
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
                retries         = config.MODBUS_RETRIES_METER,
                retry_on_empty  = True,
                baudrate        = 19200,
                bytesize        = 8,
                parity          = "N",
                stopbits        = 1,
                strict = False
                # framer=pymodbus.ModbusRtuFramer,
            ),
            1,          # Modbus address
            "meter", "SDM630 Smartmeter", 
            Eastron_SDM630.MakeRegisters() )

    async def read_coroutine( self ):
        # For power routing to work we need to read total_power frequently. So we don't read 
        # ALL registers every time. Instead, gather the unimportant ones in little groups
        # and frequently read THE important register (total_power) + one group.
        # Unimportant registers will be updated less often, who cares.
        read_fast_ctr = -1
        read_fast_regs = ((
            self.total_power                      ,    # required for fakemeter
            self.phase_1_line_to_neutral_volts    ,    # required for fakemeter
            self.phase_2_line_to_neutral_volts    ,
            self.phase_3_line_to_neutral_volts    ,
        ),(
            self.total_power                      ,    # required for fakemeter
            self.phase_1_current                  ,    # required for fakemeter
            self.phase_2_current                  ,
            self.phase_3_current                  ,
        ),(
            self.total_power                      ,    # required for fakemeter
            self.phase_1_power                    ,
            self.phase_2_power                    ,
            self.phase_3_power                    ,
        ),(
            self.total_power                      ,    # required for fakemeter
            self.total_volt_amps                  ,    # required for fakemeter
            self.total_var                        ,    # required for fakemeter
            self.total_power_factor               ,    # required for fakemeter
            self.total_phase_angle                ,    # required for fakemeter
            self.frequency                        ,    # required for fakemeter
        ),(
            self.total_power                      ,    # required for fakemeter
            self.total_import_kwh                 ,    # required for fakemeter
            self.total_export_kwh                 ,    # required for fakemeter
            self.total_import_kvarh               ,    # required for fakemeter
            self.total_export_kvarh               ,    # required for fakemeter
        ),(
            self.total_power                      ,    # required for fakemeter
            self.total_kwh                        ,    # required for fakemeter
            self.total_kvarh                      ,    # required for fakemeter
        ))

        # publish these to MQTT
        regs_to_publish = set((
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
                ))

        tick = grugbus.Metronome(config.POLL_PERIOD_METER)
        while STILL_ALIVE:
            try:
                if not self.modbus.connected:
                    await self.modbus.connect()

                data_request_timestamp = time.time()    # measure lag between modbus request and data delivered to fake smartmeter
                try:
                    read_fast_ctr = (read_fast_ctr+1) % len(read_fast_regs)
                    regs = await self.read_regs( read_fast_regs[read_fast_ctr] )
                except asyncio.exceptions.TimeoutError:
                    pub = {} # read_regs sets self.is_online to False, which sets FailSafe mode in inverter
                else:
                    self.data_timestamp = data_request_timestamp
                    pub = { reg.key: reg.format_value() for reg in regs_to_publish.intersection(regs) }      # do not publish ALL registers on mqtt

                pub[ "is_online" ]    = int( self.is_online )
                pub[ "req_time" ]     = round(time.time()-data_request_timestamp,2)   # log modbus request time, round it for better compression
                mqtt.publish( "pv/meter/", pub, add_heartbeat=True )
            except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
                return abort()
            except:
                s = traceback.format_exc()
                log.error(s)
                mqtt.mqtt.publish( "pv/exception", s )
            await tick.wait()

########################################################################################
#
#       Fronius Primo Inverter, modbus over TCP
#
########################################################################################
class Fronius( grugbus.SlaveDevice ):
    def __init__( self, ip, modbus_addr ):
        super().__init__( AsyncModbusTcpClient( ip ), modbus_addr , "fronius", "Fronius", [ 
            grugbus.registers.RegFloat( (3, 6, 16), 40095, 1, 'grid_port_power', 1, "W", 'float', None, 'Grid Port Power', '' ) 
            ] )

    async def read_coroutine( self ):
        tick = grugbus.Metronome( config.POLL_PERIOD_FRONIUS )
        try:    # It powers down at night, which disconnects TCP. 
                # pymodbus reconnects automatically, no need to put this in the while loop below
                # otherwise it will leak sockets
            await self.modbus.connect()
        except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
            return abort()

        while STILL_ALIVE:
            try:
                await tick.wait()
                try:
                    await self.grid_port_power.read()
                except (asyncio.exceptions.TimeoutError, pymodbus.exceptions.ConnectionException):
                    self.grid_port_power.value = 0.
                    pub = {}
                    # await asyncio.sleep(5)
                else:
                    self.grid_port_power.value = -(self.grid_port_power.value or 0)
                    pub = { 
                        self.grid_port_power.key: self.grid_port_power.format_value(),
                        }
                pub["is_online"] = int( self.is_online )
                mqtt.publish( "pv/fronius/", pub, add_heartbeat=True )
            except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
                return abort()
            except:
                s = traceback.format_exc()
                log.error(s)
                mqtt.mqtt.publish( "pv/exception", s )

########################################################################################
#
#       Solis inverter
#
#       COM port and local smartmeter on the same modbus interface
#       Fake meter is on its own other interface
#
########################################################################################
class Solis( grugbus.SlaveDevice ):
    def __init__( self, key, name, modbus_addr, meter, fakemeter ):
        super().__init__( meter.modbus, modbus_addr, key, name, Solis_S5_EH1P_6K_2020_Extras.MakeRegisters() )

        self.local_meter = meter        # on AC grid port
        self.fake_meter  = fakemeter    # meter emulation on meter port

        self.regs_to_read = (
            self.mppt1_voltage,              
            self.mppt1_current,              
            self.mppt2_voltage,              
            self.mppt2_current,        
            self.pv_power,      
            self.dc_bus_voltage,      
            self.temperature,

            self.battery_voltage,                    
            self.battery_current,                    
            self.battery_current_direction,          
            self.bms_battery_soc,                    
            self.bms_battery_health_soh,             
            self.bms_battery_voltage,                
            self.bms_battery_current,                
            self.bms_battery_charge_current_limit,   
            self.bms_battery_discharge_current_limit,
            self.house_load_power,                   
            self.backup_load_power,                  
            self.battery_power,                      
            self.grid_port_power,        

            self.battery_over_discharge_soc,
            self.rwr_power_on_off,              

            self.energy_generated_today,
            self.energy_generated_yesterday,
            self.battery_charge_energy_today,
            self.battery_discharge_energy_today,

            self.fault_status_1_grid              ,
            self.fault_status_2_backup            ,
            self.fault_status_3_battery           ,
            self.fault_status_4_inverter          ,
            self.fault_status_5_inverter          ,
            self.inverter_status                  ,
            self.operating_status                 ,
            self.energy_storage_mode              ,
            self.bms_battery_fault_information_01 ,
            self.bms_battery_fault_information_02 ,
            self.backup_ac_voltage                ,
            self.backup_output_enabled            ,
            self.battery_max_charge_current       ,
            self.battery_max_discharge_current    ,

            self.rwr_battery_discharge_current_maximum_setting,
            self.rwr_battery_charge_current_maximum_setting,

            self.rwr_real_time_clock_seconds,

            # self.meter_ac_voltage_a                           ,
            # self.meter_ac_current_a                           ,
            # self.meter_ac_voltage_b                           ,
            # self.meter_ac_current_b                           ,
            # self.meter_ac_voltage_c                           ,
            # self.meter_ac_current_c                           ,
            # self.meter_active_power_a                         ,
            # self.meter_active_power_b                         ,
            # self.meter_active_power_c                         ,
            # self.meter_total_active_power                     ,
            # self.meter_reactive_power_a                       ,
            # self.meter_reactive_power_b                       ,
            # self.meter_reactive_power_c                       ,
            # self.meter_total_reactive_power                   ,
            # self.meter_apparent_power_a                       ,
            # self.meter_apparent_power_b                       ,
            # self.meter_apparent_power_c                       ,
            # self.meter_total_apparent_power                   ,
            # self.meter_power_factor                           ,
            # self.meter_grid_frequency                         ,
            # self.meter_total_active_energy_imported_from_grid ,
            # self.meter_total_active_energy_exported_to_grid   ,
        )

        lm = self.local_meter
        self.local_meter_regs_to_read = ( 
                    # lm.voltage               ,
                    # lm.current               ,
                    lm.active_power          ,
                    # lm.apparent_power        ,
                    # lm.reactive_power        ,
                    # lm.power_factor          ,
                    # lm.phase_angle           ,
                    # lm.frequency             ,
                    lm.import_active_energy  ,
                    lm.export_active_energy  ,
                    # lm.import_reactive_energy,
                    # lm.export_reactive_energy,
                    # lm.total_active_energy   ,
                    # lm.total_reactive_energy ,
                )

    async def read_meter_coroutine( self ):
        tick = grugbus.Metronome( config.POLL_PERIOD_SOLIS_METER )
        while STILL_ALIVE:
            async with self.modbus._async_mutex:
                if not self.modbus.connected:
                    await self.modbus.connect()
            try:
                lm_regs = await self.local_meter.read_regs( self.local_meter_regs_to_read )
            except asyncio.exceptions.TimeoutError:
                lm_pub = {}
            except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
                return abort()
            else:
                lm_pub = { reg.key: reg.format_value() for reg in lm_regs }

            lm_pub[ "is_online" ] = int(self.local_meter.is_online)
            mqtt.publish( "pv/%s/meter/"%self.key, lm_pub, add_heartbeat=True )

            # Add useful metrics to avoid asof joins in database
            meter_pub = { "house_power" :  float(int(  (mgr.meter.total_power.value or 0)
                                                    - (self.local_meter.active_power.value or 0)
                                                    - (mgr.fronius.grid_port_power.value or 0) )) }
            mqtt.publish( "pv/meter/", meter_pub )
            mqtt.publish( "pv/", {"total_pv_power": float(int(
                              (self.pv_power.value or 0) 
                            - (mgr.fronius.grid_port_power.value or 0)))} )
            await tick.wait()

    async def read_inverter_coroutine( self ):
        tick = grugbus.Metronome( config.POLL_PERIOD_SOLIS )
        on_timer = off_timer = 0
        while STILL_ALIVE:
            async with self.modbus._async_mutex:
                if not self.modbus.connected:
                    await self.modbus.connect()
            try:
                regs = await self.read_regs( self.regs_to_read )

                # multitasking: at this point the sign of battery current and power is wrong

                # Add polarity to battery parameters
                if self.battery_current_direction.value:    # positive current/power means charging, negative means discharging
                    self.battery_current.value     *= -1
                    self.bms_battery_current.value *= -1
                    self.battery_power.value       *= -1

                # positive means inverter is consuming power, negative means producing
                # this line is commented out, we're using unit_value=-1 in the register definitions instead.
                # self.grid_port_power.value *= -1    

                # Add useful metrics to avoid asof joins in database
                pub = { reg.key: reg.format_value() for reg in regs  }
                del pub["battery_current_direction"]  # we put it in the current sign, no need to puclish it
                pub["mppt1_power"] = int( self.mppt1_current.value*self.mppt1_voltage.value )
                pub["mppt2_power"] = int( self.mppt2_current.value*self.mppt2_voltage.value )
                pub["is_online"]   = int( self.is_online )
                mqtt.publish( "pv/%s/"%self.key, pub, add_heartbeat=True )

                # Auto on/off: turn it off at night when the batery is below specified SOC
                # so it doesn't keep draining it while doing nothing useful
                if ( max( self.mppt1_voltage.value, self.mppt2_voltage.value ) < config.SOLIS_TURNOFF_MPPT_VOLTAGE 
                    and self.bms_battery_soc.value <= config.SOLIS_TURNOFF_BATTERY_SOC ):
                    off_timer += 1
                else:
                    off_timer = 0

                if max( self.mppt1_voltage.value, self.mppt2_voltage.value ) > config.SOLIS_TURNON_MPPT_VOLTAGE:
                    on_timer += 1
                else:
                    on_timer = 0

                inverter_is_on = self.rwr_power_on_off.value == 0xBE
                if inverter_is_on:
                    on_timer = 0
                    if off_timer > 500:
                        self.rwr_power_on_off.value = 0xDE     # power off
                        log.info("Powering OFF %s"%self.key)
                        await self.rwr_power_on_off.write()
                else:   # it is OFF
                    # print("OFF",on_timer)
                    off_timer = 0
                    if on_timer > 30:
                        self.rwr_power_on_off.value = 0xBE     # power on
                        log.info("Powering ON %s"%self.key)
                        await self.rwr_power_on_off.write()

                ######################################
                # Battery discharge management
                # Avoid short battery cycles 
                inverter_is_on = self.rwr_power_on_off.value == 0xBE       # in case it was changed by write above

                # self.rwr_battery_discharge_current_maximum_setting.set_value( 50 )
                # await self.rwr_battery_discharge_current_maximum_setting.write()

            except asyncio.exceptions.TimeoutError:
                pass # This also covers register writes above, not just the first read, so do not move
            except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
                return abort()
            except:
                s = traceback.format_exc()
                log.error(s)
                mqtt.mqtt.publish( "pv/exception", s )
            await tick.wait()


########################################################################################
#
#       Put it all together
#
########################################################################################
class SolisManager():
    def __init__( self ):

        self.meter = MainSmartmeter()

        # Port for inverters COM and local meters
        lmport = AsyncModbusSerialClient(
                port            = config.COM_PORT_SOLIS,
                timeout         = 0.3,
                retries         = config.MODBUS_RETRIES_METER,
                retry_on_empty  = True,
                baudrate        = 9600,
                bytesize        = 8,
                parity          = "N",
                stopbits        = 1,
                strict = False
                # framer=pymodbus.ModbusRtuFramer,
            )

        self.solis1 = Solis( "solis1", "Solis 1", 
            modbus_addr = 1, 
            meter = grugbus.SlaveDevice( lmport, 3, "ms1", "SDM120 Smartmeter on Solis 1", Eastron_SDM120.MakeRegisters() ),
            fakemeter = FakeSmartmeter( config.COM_PORT_FAKE_METER, "fake_meter_1", "Fake SDM120 for Inverter 1" )
        )

        self.fronius = Fronius( '192.168.0.17', 1 )

    ########################################################################################
    #   Start async processes
    ########################################################################################

    def start( self ):
        if sys.version_info >= (3, 11):
            # with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
            with asyncio.Runner() as runner:
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

        asyncio.create_task( self.solis1.fake_meter.start_server() )
        asyncio.create_task( abort_on_exit( self.meter.read_coroutine() ))
        asyncio.create_task( abort_on_exit( self.fronius.read_coroutine() ))
        asyncio.create_task( abort_on_exit( self.solis1.read_inverter_coroutine() ))
        asyncio.create_task( abort_on_exit( self.solis1.read_meter_coroutine() ))
        asyncio.create_task( self.display_coroutine() )
        asyncio.create_task( self.mqtt_start() )

        await STOP.wait()
        await mqtt.mqtt.disconnect()

    async def mqtt_start( self ):
        await mqtt.mqtt.connect( config.MQTT_BROKER )

    ########################################################################################
    #   Local display
    ########################################################################################

    async def display_coroutine( self ):
        while STILL_ALIVE:
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
                ):
                    r.extend( reg.get_on_bits() )


                for reg in (
                    meter.phase_1_power                    ,
                    meter.phase_2_power                    ,
                    meter.phase_3_power                    ,
                    meter.total_power               ,
                    "",
                    solis.pv_power,      
                    solis.battery_power,                      
                    self.fronius.grid_port_power,
                    solis.grid_port_power,        
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

                    solis.energy_generated_today,

                    solis.rwr_real_time_clock_seconds,

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
                            r.append( "%15s %40s %10s" % (reg.device.key, reg.key, reg.format_value() ) )

                print( "\n".join(r) )
            except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
                return abort()
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







