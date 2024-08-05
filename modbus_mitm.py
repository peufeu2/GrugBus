#!/usr/bin/python
# -*- coding: utf-8 -*-

import os, pprint, time, sys, serial, socket, traceback, struct, datetime, logging, math, traceback, shutil, collections
from path import Path

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


# This program is supposed to run on a potato (Allwinner H3 SoC) and uses async/await,
# so import the fast async library uvloop
import asyncio, signal, uvloop, aiohttp

# Modbus
import pymodbus
from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient
from pymodbus.datastore import ModbusServerContext, ModbusSlaveContext, ModbusSequentialDataBlock
from pymodbus.server import StartAsyncSerialServer
from pymodbus.transaction import ModbusRtuFramer

# MQTT
from gmqtt import Client as MQTTClient

# Device wrappers and misc local libraries
from misc import *
import grugbus
from grugbus.devices import Eastron_SDM120, Solis_S5_EH1P_6K_2020_Extras, Eastron_SDM630, Acrel_1_Phase, EVSE_ABB_Terra
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
        self.mqtt.subscribe('cmd/pv/#', qos=0)

    def on_disconnect(self, client, packet, exc=None):
        pass

    async def on_message(self, client, topic, payload, qos, properties):
        print( "MQTT", topic, payload )
        if topic=="cmd/pv/backup_output_enabled":
            v = bool(int( payload ))
            reg = mgr.solis1.rwr_backup_power_enable_setting
            await reg.read()
            print( reg.key, reg.value, "->", v )
            await reg.write( v )
        elif topic=="cmd/pv/power_on":
            v = (0xDE,0xBE) [bool(int( payload ))]
            reg = mgr.solis1.rwr_power_on_off
            await reg.read()
            print( reg.key, reg.value, "->", v )
            await reg.write( v )
        elif topic=="cmd/pv/write":
            addr,value = payload.split(b" ",1)
            addr = int(addr)
            value = int(value)
            reg = mgr.solis1.regs_by_addr.get(addr)
            if not reg:
                print("Unknown register", addr)
            await reg.read()
            print( reg.key, reg.value, "->", value )
            await reg.write( value )
            print( reg.key, reg.value )
        elif topic=="cmd/pv/read":
            addr = int(payload)
            reg = mgr.solis1.regs_by_addr.get(addr)
            if not reg:
                print("Unknown register", addr)
            await reg.read()
            print( reg.key, reg.value )
        elif topic=="cmd/pv/offset":
            v = int(payload)
            print("power_offset", v)
            mgr.solis1.fake_meter.power_offset = v
        elif topic=="cmd/pv/elimit":
            v = int(payload)
            print("elimit", v)
            mgr.solis1.fake_meter.power_elimit = v


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

        return True

mqtt = MQTT()

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
        meter = mgr.meter
        if not meter.is_online:
            log.warning( "FakeSmartmeter cannot reply to client: real smartmeter offline" )
            # return value is False so pymodbus server will abort the request, which the inverter
            # correctly interprets as the meter being offline
            return False

        try:    # Fill our registers with up-to-date data
            self.voltage                .value = meter.phase_1_line_to_neutral_volts .value
            self.current                .value = meter.phase_1_current               .value
            self.apparent_power         .value = meter.total_volt_amps               .value
            self.reactive_power         .value = meter.total_var                     .value
            self.power_factor           .value = (meter.total_power_factor            .value % 1.0)
            self.frequency              .value = meter.frequency                     .value
            self.import_active_energy   .value = meter.total_import_kwh              .value
            self.export_active_energy   .value = meter.total_export_kwh              .value
            # self.active_power           .value = max( meter.total_power                   .value + self.power_offset, -self.power_elimit ) + (time.time()%2)
            # meter placement: grid

            # report tweaked value, it shifts a little bit to have a small amount of
            # export when we can afford it, to really keep power drawn from grid to zero
            # instead of "fluctuating around zero""
            self.active_power           .value = meter.total_power_tweaked

            # meter placement: load
            # self.active_power           .value = max(0, meter.total_power.value - mgr.solis1.local_meter.active_power.value)

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
                self.active_power.key: self.active_power.format_value(), # log what we sent to the inverter
                "offset": int(self.power_offset)
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
#       This class reads the meter and publishes it on MQTT
#
#       Meter is read very often, so it gets its own serial port
#
########################################################################################
class MainSmartmeter( grugbus.SlaveDevice ):
    def __init__( self, modbus ):
        super().__init__(  
            modbus,
            1,                      # Modbus address
            "meter",                # Name (for logging etc)
            "SDM630 Smartmeter",    # Pretty name 
            # List of registers is in another file, which is auto-generated from spreadsheet
            # so import it now
            Eastron_SDM630.MakeRegisters() )

        self.router = Router()
        self.total_power_tweaked = 0.0

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
            self.average_line_to_neutral_volts_thd,
            self.average_line_current_thd         ,
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
            self.total_volt_amps                  ,    # required for fakemeter
            self.total_var                        ,    # required for fakemeter
            self.total_power_factor               ,    # required for fakemeter
            self.total_phase_angle                ,    # required for fakemeter
            self.average_line_to_neutral_volts_thd,
            self.average_line_current_thd         ,

                ))

        tick = Metronome(config.POLL_PERIOD_METER)  # fires a tick on every period to read periodically, see misc.py
        while STILL_ALIVE:  # set to false by abort()
            try:
                async with self.modbus._async_mutex:
                    if not self.modbus.connected:
                        await self.modbus.connect()

                data_request_timestamp = time.time()    # measure lag between modbus request and data delivered to fake smartmeter
                try:
                    read_fast_ctr = (read_fast_ctr+1) % len(read_fast_regs)
                    regs = await self.read_regs( read_fast_regs[read_fast_ctr] )

                    # offset measured power a little bit to ensure a small value of export
                    # even if the battery is not fully charged
                    self.total_power_tweaked = self.total_power.value
                    bp  = mgr.solis1.battery_power.value or 0       # Battery charging power. Positive if charging, negative if discharging.
                    soc = mgr.solis1.bms_battery_soc.value or 0     # battery soc, 0-100
                    if bp > 200:        self.total_power_tweaked += soc*bp*0.0001

                except asyncio.exceptions.TimeoutError:
                    # Nothing special to do: in case of error, read_regs() above already set self.is_online to False
                    pub = {}
                else:
                    self.data_timestamp = data_request_timestamp
                    pub = { reg.key: reg.format_value() for reg in regs_to_publish.intersection(regs) }      # publish what we just read
                pub[ "is_online" ]    = int( self.is_online )   # set by read_regs(), True if it succeeded, False otherwise
                pub[ "req_time" ]     = round( self.last_transaction_duration,2 )   # log modbus request time, round it for better compression
                mqtt.publish( "pv/meter/", pub, add_heartbeat=True )
                await self.router.route()
            except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
                return abort()
            except:
                s = traceback.format_exc()
                log.error(s)
                mqtt.mqtt.publish( "pv/exception", s )
                await asyncio.sleep(0.5)
            await tick.wait()

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

        self.tick_poll = Metronome( 1 )
        self.timeout = Timeout( 10, expired=True )    # expires if we've been exporting for some time

        # settings, DO NOT CHANGE as these are set in the ISO standard
        self.i_pause = 5.0          # current limit in pause mode
        self.i_min   = 6.0          # minimum current limit for charge, can be set to below 6A if charge should pause when solar not available
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
        self.p_export_target = 50

    async def poll( self, p ):
        try:
            await self.read_regs( self.regs_to_read )
            pub = { reg.key: reg.format_value() for reg in self.regs_to_read }
            pub["virtual_current_limit"] = "%.02f"%self.virtual_current_limit
            pub["fake_power"]            = int(p)
            pub["rwr_current_limit"]     = self.rwr_current_limit.format_value()
            pub["charging_unpaused"]     = self.is_charging_unpaused()
            mqtt.publish( "pv/evse/", pub, add_heartbeat=True )
            line = "%-6dW e=%6d s=%04x s=%04x v%6.03fA %6.03fA %6.03fA %6.03fA %6.03fW %6.03fWh %fs" % (p, self.error_code.value, self.charge_state.value, self.socket_state.value, 
                self.virtual_current_limit, self.rwr_current_limit.value, self.current_limit.value, self.current.value, 
                self.active_power.value, self.energy.value, self.last_transaction_duration )
            print( line )
            return True
        except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
            return abort()
        except:
            s = traceback.format_exc()
            log.error(s)
            mqtt.mqtt.publish( "pv/exception", s )
            await asyncio.sleep(0.5)

    async def route( self, excess ):
        # Poll charging station
        if not self.tick_poll.ticked():         # polling interval
            return

        # poll EVSE over modbus
        if not await self.poll( excess ):
            self.tick_poll.tick = 30.0  # If it did not respond, poll it less often to not disturb the shared smartmeter modbus line
            self.default_retries = 1
            return
        self.tick_poll.tick = 1.0   # EVSE is online, use short poll interval
        self.default_retries = 2

        # EV is not plugged, or init register at startup
        if (self.socket_state.value) != 0x111 or (not self.rwr_current_limit.value):    
            await self.set_virtual_current_limit( self.virtual_i_min )                    # set charge to paused
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

        # Now charge with solar only
        # EVSE is slow, the car takes 2 seconds to respond to commands, then the inverter needs to adjust,
        # new value of excess power needs to stabilize, etc, before a new power setting can he issued.
        # This means we have to wait about 10s between commands.
        if not self.timeout.expired(): return
        self.timeout.reset( 10 )

        if self.is_charging_paused():
            if excess > self.p_threshold_start:
                await self.set_virtual_current_limit( self.i_min )    # set current to minimum value to allow charging to begin
            return

        if self.current.value < 5.5:    
            # Current is low. This either means:
            # - Charge hasn't started yet, so we shouldn't issue new current adjustments that will be ignored... as the result of that
            # would be incrementing the current limit too much and when charging starts, a big current spike.
            # - Or it is in the final charging/balancing stage, and we want that to finish cleanly no matter what excess PV is.
            # In both cases, we just do nothing.
            return

        # Finally... adjust current
        await self.adjust_virtual_current_limit( (excess-self.p_export_target) * 0.003 )    # It is charging

    def is_charging_paused( self ):
        return self.rwr_current_limit.value < self.i_min

    def is_charging_unpaused( self ):
        return self.rwr_current_limit.value >= self.i_min

    async def adjust_virtual_current_limit( self, increment ):
        await self.set_virtual_current_limit( self.virtual_current_limit + increment )

    # Adjusting current when charging takes a few seconds so we can do it often.
    # But pausing and restarting charge is slow (>30s) so we should not do it just because there was a spike in the
    # exported power measured by the meter.
    # When current limit is 6A, EVSE will start charging. Below that it will pause.
    # So we use a virtual_current_limit that is adjusted according to excess power, then derive the real current limit from it:
    # when the virtual current limit is lower than 6A but not too low, we keep the real current limit at 6A so it keeps charging.
    # This avoids short pause/restart cycles;
    async def set_virtual_current_limit( self, value ):
        # clip it
        self.virtual_current_limit = value = min( self.virtual_i_max, max( self.virtual_i_min, value ))

        # hysteresis comparator
        threshold = self.virtual_i_pause if self.is_charging_unpaused() else self.virtual_i_unpause

        if value <= threshold:     current_limit = self.i_pause     # very low: pause charging
        elif value <= self.i_min:  current_limit = self.i_min       # low but not too low: keep charging at min power
        else:                      current_limit = value            # above 6A: send current limit unchanged

        await self.rwr_current_limit.write_if_changed( current_limit )



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

        self.excess_history_nobat = collections.deque( maxlen=70 )        # queue to smooth measured power
        self.excess_history_bat   = collections.deque( maxlen=70 )        # queue to smooth measured power

        # settings, can be changed
        self.battery_min_soc = 35               # but only if the soc is above this
        self.battery_min_charge_power = 2000    # steal power from inverter battery charging above this, it should cover the minimum of 6A
        self.solis1_max_output_power = 5700


        self.initialized = False
        self.timeout_export = Timeout( 4, expired=True )    # expires if we've been exporting for some time
        self.timeout_import = Timeout( 3, expired=True )    # expires if we've been importing for some time
        self.resend_tick = Metronome( 10 )
        self.counter = 0




        # self.off_counter = 0

    async def route( self ):
        # return
        if self.initialized < 2:
            for d in self.devices:
                d.off()
            self.initialized += 1
            return

        # Get export power from smartmeter, positive when exporting.
        # Smartmeter gives a negative valu when exporting power, so invert the sign.
        # This is the main control variable for power routing. The rest of this code is about tweaking it
        # according to operating conditions to get desired behavior.
        excess = meter_export = -mgr.meter.total_power_tweaked

        # Export power alone is not sufficient: at night when running on battery it will fluctuate
        # around zero but with spikes in import/export and we don't want that to trigger the EVSE charge!
        # What we're interested in is excess production: (meter export) + (solar battery charging power)
        # this behaves correctly in all cases.

        bp  = mgr.solis1.battery_power.value or 0       # Battery charging power. Positive if charging, negative if discharging.
        soc = mgr.solis1.bms_battery_soc.value or 0     # battery soc, 0-100

        steal_from_battery = 0
        if soc < self.battery_min_soc:
            # When below minimum SOC, only count battery discharging power, but not charging power.
            # So the inverter can charge its battery as much as it wants. 
            # (If it has too much PV and still exports power, this is already accounted for in "excess" variable.)
            # This gives priority to charging the battery, while still working properly at night:
            # When we run on battery, meter export is around zero, and battery provides all the power,
            # so bp is negative, the result is excess<0, which disables routing.
            excess += min( -100, bp )
        else:
            # Battery is above minimum SOC.
            if bp < 100:
                # Battery is discharging or idle/finished charging.
                # 100W threshold is used instead of 0 due to offset in the inverter battery power measurement.
                # Don't try to enforce minimum charging power, as it makes no sense when battery is fully charged.
                excess += bp
            else:
                # Battery is charging. Guarantee minimum battery charging power according to battery SOC
                min_bp = self.battery_min_charge_power * (150 - soc) / (150-self.battery_min_soc)
                if bp >= min_bp:  
                    # Battery is charging above minimum power, we can steal some power from it
                    # However, if solis AC output is maxed out, it may still be charging the battery, but we can't
                    # steal this power. So in this case, we shouldn't count battery charging power as available!
                    # This is done below with excess_1 and excess_2
                    steal_from_battery = max( 0, bp - min_bp)

        # We now have two excess power measurements
        #   excess                      does not include power that can be stolen from the battery
        #   excess+steal_from_battery   it includes that
        # TODO: use both to also control the water heater and other stuff.
        # For now this is just for the EVSE.

        # Store it in a deque to smooth it, to avoid jerky behavior on power spikes
        self.excess_history_nobat.append( excess )          # TODO: this was previously meter_export
        self.excess_history_bat.append( excess + steal_from_battery )

        # Smooth it
        excess_avg_nobat = sum(self.excess_history_nobat) / len(self.excess_history_nobat)
        excess_avg_bat   = sum(self.excess_history_bat)   / len(self.excess_history_bat)

        # Also get solis1 output power, negative if producing power, so flip sign to make it positive
        solis1_output = -(mgr.solis1.local_meter.active_power.value or 0)
        
        # If the inverter's grid port is maxed out, "steal_from_battery" will be too optimistic,
        # as the inverter can't redirect more power to its output in this case.
        # Correct for this:
        if excess_avg_nobat < 0 and solis1_output > self.solis1_max_output_power:
            excess_avg = min( excess_avg_nobat, excess_avg_bat )
        else:
            excess_avg = excess_avg_bat

        # We now have our excess PV power available for routing!
        # excess                        Instantaneous excess power
        # excess + steal_from_battery   Instantaneous excess power, including what can be stolen from battery charging
        # excess_avg_bat                Smoothed excess power
        # excess_avg                    Smoothed excess power, including what can be stolen from battery charging

        # Poll charging station
        evse = mgr.evse
        await evse.route( excess_avg )
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
#       Fronius Primo Inverter, modbus over TCP
#
########################################################################################
class Fronius( grugbus.SlaveDevice ):
    def __init__( self, ip, modbus_addr ):
        super().__init__( AsyncModbusTcpClient( ip ), modbus_addr , "fronius", "Fronius", [ 
            grugbus.registers.RegFloat( (3, 6, 16), 40095, 1, 'grid_port_power', 1, "W", 'float', None, 'Grid Port Power', '' ) 
            ] )

    async def read_coroutine( self ):
        tick = Metronome( config.POLL_PERIOD_FRONIUS )
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
                    # t = time.time()
                    await self.grid_port_power.read()
                    # print("Fronius", time.time()-t, self.grid_port_power.value)
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
                await asyncio.sleep(0.5)

########################################################################################
#
#       Solis inverter
#
#       COM port and local smartmeter on the same modbus interface
#       Fake meter is on its own other interface
#
########################################################################################
class Solis( grugbus.SlaveDevice ):
    def __init__( self, modbus, modbus_addr, key, name, meter, fakemeter ):
        super().__init__( modbus, modbus_addr, key, name, Solis_S5_EH1P_6K_2020_Extras.MakeRegisters() )

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
            # self.rwr_energy_storage_mode              ,
            self.bms_battery_fault_information_01 ,
            self.bms_battery_fault_information_02 ,
            self.backup_voltage                   ,
            self.backup_output_enabled            ,
            self.battery_max_charge_current       ,
            self.battery_max_discharge_current    ,

            self.rwr_battery_discharge_current_maximum_setting,
            self.rwr_battery_charge_current_maximum_setting,

            self.phase_a_voltage,
            self.rwr_backup_output_enabled,

            self.rwr_epm_export_power_limit,

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
        tick = Metronome( config.POLL_PERIOD_SOLIS_METER )
        timeout_counter = 0
        while STILL_ALIVE:
            async with self.modbus._async_mutex:
                if not self.modbus.connected:
                    await self.modbus.connect()
            try:
                lm_regs = await self.local_meter.read_regs( self.local_meter_regs_to_read )
                timeout_counter = 0
            except asyncio.exceptions.TimeoutError:
                lm_pub = {}
                self.local_meter.active_power.value = None if timeout_counter<10 else 0 # if it times out, there is no measured power
                timeout_counter += 1
            except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
                return abort()
            except:
                s = traceback.format_exc()
                log.error(s)
                mqtt.mqtt.publish( "pv/exception", s )
                await asyncio.sleep(0.5)
            else:
                lm_pub = { reg.key: reg.format_value() for reg in lm_regs }

            lm_pub[ "is_online" ] = int(self.local_meter.is_online)
            mqtt.publish( "pv/%s/meter/"%self.key, lm_pub, add_heartbeat=True )

            # Add useful metrics to avoid asof joins in database
            if mgr.meter.is_online:
                meter_pub = { "house_power" :  float(int(  (mgr.meter.total_power.value or 0)
                                                        - (self.local_meter.active_power.value or 0)
                                                        - (mgr.fronius.grid_port_power.value or 0) )) }
                mqtt.publish( "pv/meter/", meter_pub )
            mqtt.publish( "pv/", {"total_pv_power": float(int(
                              (self.pv_power.value or 0) 
                            - (mgr.fronius.grid_port_power.value or 0)))} )
            await tick.wait()

    async def read_inverter_coroutine( self ):
        async with self.modbus._async_mutex:
            if not self.modbus.connected:
                await self.modbus.connect()

        await self.adjust_time()
        tick = Metronome( config.POLL_PERIOD_SOLIS )
        timeout_fan_off = Timeout( 60 )
        bat_power_deque = collections.deque( maxlen=10 )
        timeout_power_on  = Timeout( 60, expired=True )
        timeout_power_off = Timeout( 600 )
        timeout_blackout  = Timeout( 120, expired=True )
        power_reg = self.rwr_power_on_off
        while STILL_ALIVE:
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
                pub["bms_battery_power"] = int( self.bms_battery_current.value * self.bms_battery_voltage.value )

                # Blackout logic: enable backup output in case of blackout
                blackout = self.fault_status_1_grid.bit_is_active( "No grid" ) and self.phase_a_voltage.value < 100
                if blackout:
                    log.info( "Blackout" )
                    await self.rwr_backup_output_enabled.write_if_changed( 1 )      # enable backup output
                    await self.rwr_power_on_off         .write_if_changed( self.rwr_power_on_off.value_on )   # Turn inverter on (if it was off)
                    await self.rwr_energy_storage_mode  .write_if_changed( 0x32 )   # mode = Backup, optimal revenue, charge from grid
                    timeout_blackout.reset()
                elif not timeout_blackout.expired():        # stay in backup mode with backup output enabled for a while
                    log.info( "Remain in backup mode for %d s", timeout_blackout.remain() )
                    timeout_power_on.reset()
                    timeout_power_off.reset()
                else:
                    await self.rwr_backup_output_enabled.write_if_changed( 0 )      # disable backup output
                    await self.rwr_energy_storage_mode  .write_if_changed( 0x23 )   # Self use, optimal revenue, charge from grid

                    # Auto on/off: turn it off at night when the batery is below specified SOC
                    # so it doesn't keep draining it while doing nothing useful
                    inverter_is_on = power_reg.value == power_reg.value_on
                    mpptv = max( self.mppt1_voltage.value, self.mppt2_voltage.value )
                    if inverter_is_on:
                        if mpptv < config.SOLIS_TURNOFF_MPPT_VOLTAGE and self.bms_battery_soc.value <= config.SOLIS_TURNOFF_BATTERY_SOC:
                            timeout_power_on.reset()
                            if timeout_power_off.expired():
                                log.info("Powering OFF %s"%self.key)
                                await power_reg.write_if_changed( power_reg.value_off )
                            else:
                                log.info( "Power off %s in %d s", self.key, timeout_power_off.remain() )
                        else:
                            timeout_power_off.reset()
                    else:
                        if mpptv > config.SOLIS_TURNON_MPPT_VOLTAGE:                        
                            timeout_power_off.reset()
                            if timeout_power_on.expired():
                                log.info("Powering ON %s"%self.key)
                                await power_reg.write_if_changed( power_reg.value_on )
                            else:
                                log.info( "Power on %s in %d s", self.key, timeout_power_on.remain() )
                        else:
                            timeout_power_on.reset()

                ######################################
                # Battery discharge management
                # Avoid short battery cycles 
                inverter_is_on = power_reg.value == power_reg.value_on       # in case it was changed by write above

                # self.rwr_battery_discharge_current_maximum_setting.set_value( 50 )
                # await self.rwr_battery_discharge_current_maximum_setting.write()

                # start fan if temperature too high or average battery power high
                bat_power_deque.append( abs(pub["bms_battery_power"]) )
                if self.temperature.value > 40 or sum(bat_power_deque) / len(bat_power_deque) > 2000:
                    timeout_fan_off.reset()
                    mqtt.publish( "cmnd/plugs/tasmota_t3/", {"Power": "1"} )
                    # print("Fan ON")
                elif self.temperature.value < 35 and timeout_fan_off.expired():
                    mqtt.publish( "cmnd/plugs/tasmota_t3/", {"Power": "0"} )
                    # print("Fan OFF")

                mqtt.publish( "pv/%s/"%self.key, pub, add_heartbeat=True )

            except asyncio.exceptions.TimeoutError:
                pass # This also covers register writes above, not just the first read, so do not move
            except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
                return abort()
            except:
                s = traceback.format_exc()
                log.error(s)
                mqtt.mqtt.publish( "pv/exception", s )
                await asyncio.sleep(0.5)
            await tick.wait()

    def get_time_regs( self ):
        return ( self.rwr_real_time_clock_year, self.rwr_real_time_clock_month,  self.rwr_real_time_clock_day,
         self.rwr_real_time_clock_hour, self.rwr_real_time_clock_minute, self.rwr_real_time_clock_seconds )

    async def get_time( self ):
        regs = self.get_time_regs()
        await self.read_regs( regs )
        dt = [ reg.value for reg in regs ]
        dt[0] += 2000
        return datetime.datetime( *dt )

    async def set_time( self, dt  ):
        self.rwr_real_time_clock_year   .value = dt.year - 2000   
        self.rwr_real_time_clock_month  .value = dt.month   
        self.rwr_real_time_clock_day    .value = dt.day   
        self.rwr_real_time_clock_hour   .value = dt.hour   
        self.rwr_real_time_clock_minute .value = dt.minute   
        self.rwr_real_time_clock_seconds.value = dt.second
        await self.write_regs( self.get_time_regs() )

    async def adjust_time( self ):
        inverter_time = await self.get_time()
        dt = datetime.datetime.now()
        log.info( "Inverter time: %s, Pi time: %s" % (inverter_time.isoformat(), dt.isoformat()))
        deltat = abs( dt-inverter_time )
        if deltat < datetime.timedelta( seconds=2 ):
            log.info( "Inverter time is OK, we won't set it." )
        else:
            if deltat > datetime.timedelta( seconds=4000 ):
                log.info( "Pi time seems old, is NTP active?")
            else:
                log.info( "Setting inverter time to Pi time" )
                await self.set_time( dt )
                inverter_time = await self.get_time()
                log.info( "Inverter time: %s, Pi time: %s" % (inverter_time.isoformat(), dt.isoformat()))


########################################################################################
#
#       Put it all together
#
########################################################################################
class SolisManager():
    def __init__( self ):

        main_meter_modbus = AsyncModbusSerialClient(    # open Modbus on serial port
                port            = config.COM_PORT_METER,
                timeout         = 1,
                retries         = config.MODBUS_RETRIES_METER,
                retry_on_empty  = True,
                baudrate        = 19200,
                bytesize        = 8,
                parity          = "N",
                stopbits        = 1,
                strict = False
                # framer=pymodbus.ModbusRtuFramer,
            )
        self.meter = MainSmartmeter( main_meter_modbus )
        self.evse  = EVSE( main_meter_modbus )

        # Port for inverters COM and local meters
        local_meter_modbus = AsyncModbusSerialClient(
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
        self.solis1 = Solis( 
            modbus=local_meter_modbus, 
            modbus_addr = 1, 
            key = "solis1", name = "Solis 1", 
            meter = grugbus.SlaveDevice( local_meter_modbus, 3, "ms1", "SDM120 Smartmeter on Solis 1", Eastron_SDM120.MakeRegisters() ),
            fakemeter = FakeSmartmeter( config.COM_PORT_FAKE_METER1, "fake_meter_1", "Fake SDM120 for Inverter 1" )
        )

        self.fronius = Fronius( '192.168.0.17', 1 )

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

        asyncio.create_task( self.solis1.fake_meter.start_server() )
        asyncio.create_task( abort_on_exit( self.meter.read_coroutine() ))
        asyncio.create_task( abort_on_exit( self.fronius.read_coroutine() ))
        asyncio.create_task( abort_on_exit( self.solis1.read_inverter_coroutine() ))
        asyncio.create_task( abort_on_exit( self.solis1.read_meter_coroutine() ))
        asyncio.create_task( abort_on_exit( self.sysinfo_coroutine() ))
        asyncio.create_task( self.display_coroutine() )
        asyncio.create_task( self.mqtt_start() )

        app = aiohttp.web.Application()
        app.add_routes([aiohttp.web.get('/', self.webresponse), aiohttp.web.get('/solar_api/v1/GetInverterRealtimeData.cgi', self.webresponse)])
        runner = aiohttp.web.AppRunner(app, access_log=False)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner,host=config.SOLARPI_IP,port=8080)
        await site.start()

        await STOP.wait()
        await mqtt.mqtt.disconnect()

    ########################################################################################
    #   Web server
    ########################################################################################

    async def webresponse( self, request ):
        p = (self.solis1.pv_power.value or 0) - (self.fronius.grid_port_power.value or 0)
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
        while STILL_ALIVE:
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
                return abort()
            except:
                log.error(traceback.format_exc())

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
                    self.fronius.grid_port_power,
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







