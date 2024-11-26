#!/usr/bin/env python3

import asyncio, struct, uvloop, time, logging, collections, copy, sys
import can
from path import Path
from misc import *
from pv.mqtt_wrapper import MQTTWrapper, MQTTSetting, MQTTVariable
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
        pm = cls._subclasses[msg.arbitration_id]( msg.data )
        # store original message and attributes
        pm.can_msg = msg
        pm.arbitration_id = msg.arbitration_id
        pm.is_extended_id = msg.is_extended_id
        return pm

    def to_can_message( self ):
        return can.Message( arbitration_id=self.arbitration_id, data=self.encode(), is_extended_id=self.is_extended_id )

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
    _members = "charge_voltage", "max_charge_current", "max_discharge_current", "reserved"
    def decode( self, data ):
        r = struct.unpack( "<Hhhh", data )
        self.charge_voltage        = r[0] * 0.1
        self.max_charge_current    = r[1] * 0.1
        self.max_discharge_current = r[2] * 0.1
        self.reserved              = r[3]

    def encode( self ):
        return bytearray( struct.pack( "<Hhhh", int(10 * self.charge_voltage), int(10 * self.max_charge_current), int(10 * self.max_discharge_current), self.reserved ))

class PylonSOCMessage( PylonMessage ):
    can_id = 0x355
    _members = "soc","soh"
    def decode( self, data ):
        self.soc = struct.unpack("<H", data[:2])[0]
        self.soh = struct.unpack("<H", data[2:4])[0]

    def encode( self ):
        return bytearray( struct.pack( "<HH", int( self.soc ), int( self.soh ) ))


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
        # If it's a PylonMessage, encode it into can.Message while preserving arbitration id and extended attr
        if isinstance( msg, PylonMessage ):
            msg = msg.to_can_message()
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
    def __init__( self, channel, key, mqtt ):
        super().__init__( channel )
        self.key = key
        self.mqtt = mqtt
        self.mqtt_topic = "pv/%s/" % key
        MQTTVariable( self.mqtt_topic+"battery_current", self, "battery_current", float, None, 0 )

    async def handle( self, msg ):
        self.can_bat.to_battery_queue.append( msg )

class PylonCAN( AsyncCAN ):
    def __init__( self, channel, mqtt ):
        super().__init__( channel )
        self.mqtt = mqtt
        self.mqtt_topic = "pv/bms/"

        self.to_battery_queue = collections.deque( maxlen=4 )
        self.echo_tick = Metronome( 1 )
        self.dispatch = {}
        for cls in PylonMessage._subclasses.values():
            setattr( self, cls.__name__, None )
            self.dispatch[ cls ] = getattr(self, "handle_"+cls.__name__, None )

        MQTTSetting( self, "charge_current_limit"        , float, lambda x: (0.0<=x<=300), 300, self.setting_updated )
        MQTTSetting( self, "discharge_current_limit"     , float, lambda x: (0.0<=x<=300), 300, self.setting_updated )

    async def setting_updated( self, setting ):
        print( setting.mqtt_topic, setting.prev_value, setting.value )
        self.handle_PylonLimitsMessage( self.PylonLimitsMessage )

    async def handle( self, msg ):
        pm = PylonMessage.load(msg)         # parse it
        setattr( self, pm.__class__.__name__, pm )  # store last received message using class name

        # process it (code below is in order of message publication)
        f = self.dispatch.get( pm.__class__ )
        if f:
            print( pm.__class__.__name__ )
            f( pm )
        else:
            for inverter in self.can_inverters:
                inverter.trysend( msg )
        
        # send inverter messages back to battery, eliminate duplicates, preserve order
        if self.echo_tick.ticked():
            sent_msgs = set()
            while self.to_battery_queue:
                m = self.to_battery_queue.popleft()
                key = (m.arbitration_id, bytes(m.data))
                if key not in sent_msgs:
                    # print( "echo  ", m )
                    sent_msgs.add( key )
                    self.bus.send( m )
                else:
                    pass
                    # print( "ignore", m )
    
    # redirect all incoming messages to other bus
        # self.notifier.add_listener( can.RedirectReader( bus ))

    def handle_PylonLimitsMessage( self, pm ):
        # apply limits
        pm.max_charge_current    = min( pm.max_charge_current,    self.charge_current_limit   .value )
        pm.max_discharge_current = min( pm.max_discharge_current, self.discharge_current_limit.value )

        mqtt.publish_value( "pv/bms/max_charge_voltage",    round( pm.charge_voltage, 1 ))
        mqtt.publish_value( "pv/bms/max_charge_current",    round( pm.max_charge_current, 1 ))
        mqtt.publish_value( "pv/bms/max_discharge_current", round( pm.max_discharge_current, 1 ))

        if self.PylonMeasurementsMessage:
            mqtt.publish_value( "pv/bms/max_charge_power",    self.PylonMeasurementsMessage.voltage * pm.max_charge_current, int )
            mqtt.publish_value( "pv/bms/max_discharge_power", self.PylonMeasurementsMessage.voltage * pm.max_discharge_current, int )

        # adjust limits for 2 inverters
        pm.max_charge_current *= 0.5
        pm.max_discharge_current *= 0.5
        pm.print()

        for inverter in self.can_inverters:
            pm2 = PylonMessage.load(pm.can_msg)  # copy message
            inverter.trysend( pm2 )

    def handle_PylonSOCMessage( self, pm ):
        pm.print()
        mqtt.publish_value( "pv/bms/soc",    pm.soc )
        mqtt.publish_value( "pv/bms/soh",    pm.soh )

        # for inverter in self.can_inverters:
        #     pm2 = PylonMessage.load(pm.can_msg)  # copy message
        #     pm2.soc = min( 97, pm.soc )
        #     inverter.trysend( pm2 )

        for inverter in self.can_inverters:
            inverter.trysend( pm )

    def handle_PylonMeasurementsMessage( self, pm ):
        pm.print()
        mqtt.publish_value( "pv/bms/power",       pm.current*pm.voltage, int )
        mqtt.publish_value( "pv/bms/voltage",     round( pm.voltage, 2 ))
        mqtt.publish_value( "pv/bms/current",     round( pm.current, 1 ))
        mqtt.publish_value( "pv/bms/temperature", round( pm.temperature, 1 ))

        for inverter in self.can_inverters:
            pm2 = PylonMessage.load(pm.can_msg)  # copy message
            pm2.current = inverter.battery_current.value    # add inverter's own reported battery current
            inverter.trysend( pm2 )

    def handle_PylonActionMessage( self, pm ):
        for name in [
            "request_full_charge",
            "request_force_charge_2",
            "request_force_charge_1",
            "discharge_enable",
            "charge_enable",
            ]:
            mqtt.publish_value( "pv/bms/"+name, getattr( pm, name ), int )

        for inverter in self.can_inverters:
            inverter.trysend( pm.can_msg )

    def handle_PylonErrorsMessage( self, pm ):
        mqtt.publish_value( "pv/bms/protection", pm.protection )
        mqtt.publish_value( "pv/bms/alarm"     , pm.alarm )

        for inverter in self.can_inverters:
            inverter.trysend( pm.can_msg )



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
    can_bat    = PylonCAN( config.CAN_PORT_BATTERY, mqtt )
    can_solis1 = SolisCAN( config.SOLIS["solis1"]["CAN_PORT"], "solis1", mqtt )
    can_solis1.can_bat = can_bat
    can_solis2 = SolisCAN( config.SOLIS["solis2"]["CAN_PORT"], "solis2", mqtt )
    can_solis1.can_bat = can_bat
    can_solis2.can_bat = can_bat
    can_bat.can_inverters = (can_solis1, can_solis2 )

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
    log.info("######################### START %s #########################", __file__ )
    start()
finally:
    with open("mqtt_stats/test_can.txt","w") as f:
        mqtt.write_stats( f )




