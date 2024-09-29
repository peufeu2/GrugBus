#!/usr/bin/env python3

import asyncio, struct, uvloop, time, threading, logging, collections, copy, sys
import can
from path import Path
from misc import *
from pv.mqtt_wrapper import MQTTWrapper
import config

logging.basicConfig( encoding='utf-8', 
                     level=logging.INFO,
                     format='[%(asctime)s] %(levelname)s:%(message)s',
                     handlers=[
                            # logging.handlers.RotatingFileHandler(Path(__file__).stem+'.log', mode='a', maxBytes=5*1024*1024, backupCount=2, encoding=None, delay=False),
                            logging.FileHandler(filename=Path(__file__).stem+'.log'), 
                            logging.StreamHandler(stream=sys.stdout)
                    ])
log = logging.getLogger(__name__)

#
#   sudo ip link set dev cana up type can bitrate 500000
#

##########################################################################
#
#           CAN messages sent by battery
#
##########################################################################

class PylonMessage:
    _data_length = 8
    _subclasses = ()    # initialized later
    _members    = ()    # initialized in child classes

    @classmethod
    def load( cls, msg ):
        return cls._subclasses[msg.arbitration_id]( msg.data )

    def __init__( self, data=None ):
        self.decode( data or bytearray(self._data_length) )

    def decode_bitfields( self, data ):
        for offset, bit, label in self._bitdefs:
            setattr( self, label, bool(data[offset] & bit) )

    def encode_bitfields( self, data ):
        for offset, bit, label in self._bitdefs:
            if getattr( self, label, 0 ):
                data[offset] |= bit

    def print( self ):
        for label in self._members:
            print( "%30s: %s" % (label, getattr(self,label)))

    # Preprocess Bitfield Definitions for Pylon CAN
    @classmethod
    def _make_bitfields( cls, definitions ):
        bittoname = {}
        nametobit = {}
        bitdefs = []
        for offset, bit, label in definitions:
            if label:   label = label.lower().replace(" ","_")
            else:       label = "reserved_%d_%d" % (offset,bit)
            bittoname[(offset,bit)] = label
            nametobit[label] = (offset,bit)
            bitdefs.append((offset, 1<<bit, label ))
        return bittoname, nametobit, bitdefs

class PylonErrorsMessage( PylonMessage ):
    can_id = 0x359
    _bittoname, _nametobit, _bitdefs = PylonMessage._make_bitfields([
            [0,  0,  "" ],
            [0,  1,  "Cell or module over voltage" ],
            [0,  2,  "Cell or module under voltage" ],
            [0,  3,  "Cell over temperature" ],
            [0,  4,  "Cell under temperature" ],
            [0,  5,  "" ],
            [0,  6,  "" ],
            [0,  7,  "Discharge over current" ],
             #,
            [1,  0,  "Charge over current" ],
            [1,  1,  "" ],
            [1,  2,  "" ],
            [1,  3,  "System error" ],
            [1,  4,  "" ],
            [1,  5,  "" ],
            [1,  6,  "" ],
            [1,  7,  "" ],
            #  ,
            [2,  0,  "" ],
            [2,  1,  "Cell or module high voltage" ],
            [2,  2,  "Cell or module low voltage" ],
            [2,  3,  "Cell high temperature" ],
            [2,  4,  "Cell low temperature" ],
            [2,  5,  "" ],
            [2,  6,  "" ],
            [2,  7,  "Discharge high current" ],
            #  ,
            [3,  0,  "Charge high current" ],
            [3,  1,  "" ],
            [3,  2,  "" ],
            [3,  3,  "Internal communication fail" ],
            [3,  4,  "" ],
            [3,  5,  "" ],
            [3,  6,  "" ],
            [3,  7,  "" ],
        ])
    _members = ["protection","alarm","module_number","P","N"] + list(_nametobit)

    def decode( self, data ):
        r = [[],[]]
        for offset, bit, label in self._bitdefs:
            if (b:=bool(data[offset] & bit)):
                r[offset>>1].append(label)
            setattr( self, label, b )
        self.protection, self.alarm = struct.unpack( "<HH", data[0:4] )
        self.module_number = data[4]
        self.P = data[5]
        self.N = data[6]

    def print( self ):
        for label in self._members:
            print( "%30s: %s" % (label, getattr(self,label)))

class PylonLimitsMessage( PylonMessage ):
    can_id = 0x351
    _members = "battery_charge_voltage", "charge_current_limit", "discharge_current_limit", "reserved"
    def decode( self, data ):
        r = struct.unpack( "<Hhhh", data )
        self.battery_charge_voltage  = r[0] * 0.1
        self.charge_current_limit    = r[1] * 0.1
        self.discharge_current_limit = r[2] * 0.1
        self.reserved                = r[3]

    def encode( self ):
        return bytearray( struct.pack( "<Hhhh", int(10 * self.battery_charge_voltage), int(10 * self.charge_current_limit), int(10 * self.discharge_current_limit), self.reserved ))

class PylonSOCMessage( PylonMessage ):
    can_id = 0x355
    _members = "soc","soh"
    def decode( self, data ):
        self.soc = struct.unpack("<H", data[:2])[0]
        self.soh = struct.unpack("<H", data[2:4])[0]

class PylonMeasurementsMessage( PylonMessage ):
    can_id = 0x356
    _members = "voltage","current","temperature"
    def decode( self, data ):
        r = struct.unpack( "<hhh", data )
        self.voltage     = r[0] * 0.01
        self.current     = r[1] * 0.1
        self.temperature = r[2] * 0.1

    def encode( self ):
        return bytearray( struct.pack( "<hhh", int(100 * self.voltage), int(10 * self.current), int(10 * self.temperature) ))


class PylonActionMessage( PylonMessage ):
    can_id = 0x35C
    _data_length = 2
    _bittoname, _nametobit, _bitdefs = PylonMessage._make_bitfields([
            [0,  0,  "" ],
            [0,  1,  "" ],
            [0,  2,  "" ],
            [0,  3,  "Request full charge" ],
            [0,  4,  "Request force charge 2" ],
            [0,  5,  "Request force charge 1" ],
            [0,  6,  "Discharge enable" ],
            [0,  7,  "Charge enable" ],
        ])
    _members = list(_nametobit)
    def decode( self, data ):
        self.decode_bitfields( data )

class PylonManufacturerMessage( PylonMessage ):
    can_id = 0x35E
    _members = "manufacturer",
    def decode( self, data ):
        self.manufacturer = data.decode("ascii")
    def print( self ):
        print( "%30s: %s" % ("manufacturer", repr(self.manufacturer)))

##########################################################################
#
#           CAN messages sent by inverter
#
##########################################################################

# inverter reply is all zeros
class InverterReplyMessage( PylonMessage ):
    can_id = 0x305
    _members = "data",
    def decode( self, data ):
        self.data = data
    def print( self ):
        pass

PylonMessage._subclasses = { cls.can_id: cls for cls in [ PylonErrorsMessage, PylonLimitsMessage, PylonSOCMessage, PylonMeasurementsMessage, PylonActionMessage, PylonManufacturerMessage, InverterReplyMessage ]}

##########################################################################
#
#           
#
##########################################################################

#   Add error handling to Notifier
#
class Notifier_e (can.Notifier):
    def _on_message_available(self, bus):
        try:
            super()._on_message_available(bus)
        except BaseException as exc:
            if not self._on_error(exc):
                # If it was not handled, raise the exception here
                raise
        # except BaseException as exc:
            # self._on_error(exc):
            # raise

#   Add error handling to AsyncBufferedReader
#
class AsyncBufferedReader_e( can.AsyncBufferedReader ):
    def on_error( self, exc ):
        self.buffer.put_nowait( exc )

    async def __anext__(self):
        m = await self.buffer.get()
        if isinstance( m, BaseException ):
            raise m
        return m

##########################################################################
#
#           Async CAN stuff
#
##########################################################################

# Base class
class AsyncCAN:
    def __init__( self, channel ):
        self.channel  = channel
        self.notifier = None
        self.bus      = None
        self.queue    = None

    async def connect( self ):
        if not self.bus:
            self.bus      = can.ThreadSafeBus( channel=self.channel, interface="socketcan", bitrate=500000)
            self.queue    = AsyncBufferedReader_e()
            self.notifier = Notifier_e( bus=self.bus, listeners=[ self.queue ], timeout=1.0, loop = asyncio.get_running_loop() )

    async def disconnect( self ):
        if self.bus:        
            if self.notifier:   
                self.notifier.stop()
            self.bus.shutdown()
        self.notifier = None
        self.bus      = None
        self.queue    = None

    def trysend( self, msg ):
        try:
            self.bus.send( msg )
        except Exception as e:
            log.error( "CAN: %s %s", self.channel, e )

    async def read_coroutine( self ):
        while True:
            try:
                if not self.bus:
                    await self.connect()
                    async for msg in self.queue:
                        await self.handle(msg)

            except Exception as e:
                log.exception( "CAN: %s %s", self.channel, e )
                try:
                    await self.disconnect()
                except Exception as e:
                    log.error( "CAN: %s %s", self.channel, e )
            await asyncio.sleep(5)

# Add feature: redirect inverter CAN messages to battery
class SolisCAN( AsyncCAN ):
    async def handle( self, msg ):
        self.can_bat.inverter_queue.append( msg )

class PylonCAN( AsyncCAN ):
    def __init__( self, channel ):
        super().__init__( channel )
        self.inverter_queue = collections.deque( maxlen=4 )
        self.echo_tick = Metronome( 1 )
        self.dispatch = {}
        for cls in PylonMessage._subclasses.values():
            setattr( self, cls.__name__, None )
            self.dispatch[ cls ] = getattr(self, "handle_"+cls.__name__, None )

    def handle_PylonLimitsMessage( self, msg, pm ):
        pm.print()
        mqtt.publish_value( "pv/battery/max_charge_voltage",    round( pm.battery_charge_voltage, 1 ))
        mqtt.publish_value( "pv/battery/max_charge_current",    round( pm.charge_current_limit, 1 ))
        mqtt.publish_value( "pv/battery/max_discharge_current", round( pm.discharge_current_limit, 1 ))

        ret = PylonMessage.load( msg )
        ret.charge_current_limit *= 0.5
        ret.discharge_current_limit *= 0.5
        # pm.print()
        return can.Message( arbitration_id=msg.arbitration_id, data=ret.encode(), is_extended_id=msg.is_extended_id )

    def handle_PylonSOCMessage( self, msg, pm ):
        pm.print()
        mqtt.publish_value( "pv/battery/soc",    pm.soc )
        mqtt.publish_value( "pv/battery/soh",    pm.soh )
        return msg

    def handle_PylonMeasurementsMessage( self, msg, pm ):
        pm.print()
        mqtt.publish_value( "pv/battery/power",       pm.current*pm.voltage, int )
        mqtt.publish_value( "pv/battery/voltage",     round( pm.voltage, 2 ))
        mqtt.publish_value( "pv/battery/current",     round( pm.current, 1 ))
        mqtt.publish_value( "pv/battery/temperature", round( pm.temperature, 1 ))

        if self.PylonLimitsMessage:
            mqtt.publish_value( "pv/battery/max_charge_power",    pm.voltage * self.PylonLimitsMessage.charge_current_limit, int )
            mqtt.publish_value( "pv/battery/max_discharge_power", pm.voltage * self.PylonLimitsMessage.discharge_current_limit, int )

    def handle_PylonActionMessage( self, msg, pm ):
        for name in [
            "request_full_charge",
            "request_force_charge_2",
            "request_force_charge_1",
            "discharge_enable",
            "charge_enable",
            ]:
            mqtt.publish_value( "pv/battery/"+name, getattr( pm, name ), int )
        return msg

    def handle_PylonErrorsMessage( self, msg, pm ):
        mqtt.publish_value( "pv/battery/protection", pm.protection )
        mqtt.publish_value( "pv/battery/alarm"     , pm.alarm )
        return msg

    async def handle( self, msg ):
        # print( msg )
        # send battery messages to inverter

        # parse it and store it
        pm = PylonMessage.load(msg)
        setattr( self, pm.__class__.__name__, pm )

        # process it (code below is in order of message publication)
        f = self.dispatch.get( pm.__class__ )
        if f:
            msg2 = f( msg, pm )
        else:
            msg2 = can.Message( arbitration_id=msg.arbitration_id, data=msg.data, is_extended_id=msg.is_extended_id )
        
        # do not handle errors on send here, instead ignore them as triggering an exception would reconnect our bus
        # which si the wrong one! Reconnection for inverter CAN bus is handled in the SolisCAN coroutine
        if msg2:
            for inverter in self.can_inverters:
                inverter.trysend( msg2 )

        # send inverter messages back to battery, eliminate duplicates, preserve order
        if self.echo_tick.ticked():
            sent_msgs = set()
            while self.inverter_queue:
                m = self.inverter_queue.popleft()
                key = (m.arbitration_id, bytes(m.data))
                if key not in sent_msgs:
                    print( "echo  ", m )
                    sent_msgs.add( key )
                    self.bus.send( m )
                else:
                    print( "ignore", m )
    
    # redirect all incoming messages to other bus
        # self.notifier.add_listener( can.RedirectReader( bus ))

##########################################################################
#
#           Main coroutine
#
##########################################################################

class MQTT( MQTTWrapper ):
    def __init__( self ):
        super().__init__( "pvcan" )

mqtt = MQTT()

async def astart():
    await mqtt.mqtt.connect( config.MQTT_BROKER_LOCAL )
    can_bat    = PylonCAN( config.CAN_PORT_BATTERY )
    can_solis1 = SolisCAN( config.CAN_PORT_SOLIS1 )
    can_solis1.can_bat = can_bat
    can_solis2 = SolisCAN( config.CAN_PORT_SOLIS2 )
    can_solis1.can_bat = can_bat
    can_solis2.can_bat = can_bat
    can_bat.can_inverters = (can_solis1, can_solis2)

    async with asyncio.TaskGroup() as tg:
        tg.create_task( can_bat   .read_coroutine() )
        tg.create_task( can_solis1.read_coroutine() )
        tg.create_task( can_solis2.read_coroutine() )


# # Connect to CAN adapters
# bat_bus   = can.interface.Bus(channel='can_bat', interface='socketcan', bitrate=500000)
# solis1_bus = can.interface.Bus(channel='can_1',   interface='socketcan', bitrate=500000)
# # can_solis2_bus = can.interface.Bus(channel='can_2',   interface='socketcan', bitrate=500000)

# # Add piping to bring data from the CAN threads (one per adapter) to the single threaded asyncio environmebt
# bat_queue = can.AsyncBufferedReader()
# can_solis1_notifier = can.Notifier(bus=solis1_bus, listeners=[can_solis1_callback], timeout=1.0)
# # can_solis2_notifier = can.Notifier(bus=can_solis2_bus, listeners=[can_solis2_callback], timeout=0)


def start():
    with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
        runner.run(astart())

try:
    print("Main thread", threading.get_ident())
    start()
finally:
    with open("mqtt_stats/test_can.txt","w") as f:
        mqtt.write_stats( f )




