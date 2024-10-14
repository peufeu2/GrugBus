#!/usr/bin/python
# -*- coding: utf-8 -*-

import asyncio, os, time, traceback, collections, logging, sys, datetime, logging, uvloop, orjson, shutil
from path import Path
from asyncio import get_event_loop
from asyncio.exceptions import TimeoutError, CancelledError
import serial_asyncio

from OPi import GPIO

from pv.mqtt_wrapper import MQTTWrapper, MQTTSetting, MQTTVariable
from misc import *
import config
import pv.reload

logging.basicConfig( encoding='utf-8', 
                     level=logging.INFO,
                     format='[%(asctime)s] %(levelname)s:%(message)s',
                     handlers=[
                            logging.FileHandler(filename=Path(__file__).stem+'.log'), 
                            logging.StreamHandler(stream=sys.stdout)
                    ])
log = logging.getLogger(__name__)

def clip01( x ):
    return min( 1, max( 0, x ))

#
#   Raspberry Pi Pico on carrier board
#
class PiPico:
    def __init__( self ):
        self.gpio_configured = False
        self.ready = False
        self.button_state = None
        self.prev_tach_time = None
        self.fan_rpm = [0] * 4
        self.fan_pwm = [0] * 4

    def start( self ):
        with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
            runner.run(self.astart())

    # Pulse GPIO tied to Pico RESET
    # it will then run boot.py
    async def hard_reset( self ):
        if not self.gpio_configured:
            GPIO.setmode( GPIO.SUNXI )     # use pin numbers on header instead of port/pin numbers
            GPIO.setup( "PG9", GPIO.OUT )

        log.info("Hard reset Pico")
        GPIO.output( "PG9", 0 )        # reset pico
        await asyncio.sleep( 0.010 )
        GPIO.output( "PG9", 1 )        # deassert reset
        self.ready = False

    # grabs one line from serial, returns None if timeout
    # also parses out of band status info
    async def serial_readline( self, wait=True ):
        async for line in self.reader:
            if line is None:
                return
            line = line.decode().strip()
            # print( "pico:", line )
            if line.startswith( "!" ):              # Exception message
                self.ready = True
                log.error( "Pico: %s" % line[1:] )
                raise Exception( line[1:] )
            elif line.startswith( "#" ):            # Log message
                self.ready = True
                log.info( "Pico: %s" % line[1:] )
            elif line.startswith( ">" ):            # status message from main.py
                self.ready = True
                self.parse_status( line[1:].strip() )
                return line
            elif line.startswith( "bootloader>" ):            # status message from main.py
                self.ready = True
                if not wait:
                    return line
            else:
                return line

    # sends a line
    async def send( self, s ):
        # print(s)
        self.writer.write( s.encode() )
        await self.writer.drain()

    # Wait to receive status info and purge it
    async def wait_ready( self ):
        self.ready = False
        while not self.ready:
            await self.serial_readline( False )

    # Upload code
    async def bootloader_put_file( self, fname ):
        log.info( "put %s", fname )
        with open( "pico/"+fname, "rb" ) as infile:
            await self.send( "put %s\n" % fname )
            while True:
                chunk_size = int( await self.serial_readline() )
                data = infile.read( chunk_size )
                await self.send( "%d\n" % len(data) )
                self.writer.write( data )
                await self.writer.drain()

                if len(data) < chunk_size:
                    break
        await self.wait_ready()

    # Exit bootloader and run main.py
    async def exit_bootloader( self ):
        await self.send( "run\n" )
        await asyncio.sleep( 0.2 )
        await self.wait_ready()

    async def set_relays( self, relay1, relay2 ):
        await self.send( "relays %d %d\n" % (relay1, relay2))

    async def set_leds( self, led_duty ):
        await self.send( "leds %s\n" % " ".join( str(int(clip01(1-d)*65535)) for d in led_duty ))

    async def set_fans( self, fans_duty ):
        await self.send( "fans %s\n" % " ".join( str(int(clip01(1-d)*65535)) for d in fans_duty ))

    async def beep( self, freq ):
        await self.send( "beep %s\n" % freq )

    def parse_status( self, line ):
        if not line:
            return
        key, value = line.split(" ",1)
        if key == "button":
            button = int( value )
            if self.button_state != None and self.button_state != button:
                self.button_state = button
                self.on_button( )
        elif key == "tach":
            self.on_tach( orjson.loads( value ) )

    def on_button( self ):
        print( "Button", self.button_state )

    def on_tach( self, tach ):
        t = time.monotonic()
        if self.prev_tach_time != None:
            age = (t - self.prev_tach_time)
            if age > 1:
                f = 30/age
                for n in range(4):
                    self.fan_rpm[n] = int( tach[n] * f )
                if self.mqtt:
                    self.mqtt.publish_value( "pv/solis1/fan_rpm", min( self.fan_rpm[0:2] ), int )
                    self.mqtt.publish_value( "pv/solis2/fan_rpm", min( self.fan_rpm[2:4] ), int )
                print( "Fan RPM:", self.fan_rpm )
        self.prev_tach_time = t

    async def on_mqtt_update_fan( self, param ):
        if param is self.solis1_fan:
            self.fan_pwm[0] = clip01( param.value * 0.01 * config.FAN_SPEED["left_fan"] )
            self.fan_pwm[1] = clip01( param.value * 0.01 )
        if param is self.solis2_fan:
            self.fan_pwm[2] = clip01( param.value * 0.01 * config.FAN_SPEED["left_fan"] )
            self.fan_pwm[3] = clip01( param.value * 0.01 )
        print("set fans", self.fan_pwm)
        await self.set_fans( self.fan_pwm )

    def on_mqtt_event_led( self, led ):
        cmd = "led %d 2\n" % led
        async def cb( topic, payload, qos, properties ):
            await self.send( cmd )
        return cb

    async def log_coroutine( self, title, fut ):
        log.info("Start:"+title )
        try:        await fut
        finally:    log.info("Exit: "+title )

    async def astart( self ):

        self.reader, self.writer = await serial_asyncio.open_serial_connection(
            url=config.MAINBOARD_SERIAL_PORT,
            baudrate=112500,
            bytesize=8,
            parity='N',
            stopbits=1,
            timeout=0.1,
            xonxoff=0,
            rtscts=0 
        )

        self.mqtt = None

        #
        #   Upload code. Command/response communication.
        #
        if "upload" in sys.argv:
            log.info("######################### UPLOAD #########################")
            await self.hard_reset()
            await self.wait_ready()               # consume garbage generated on serial during reset
            await self.bootloader_put_file( "boot.py" )
            await self.bootloader_put_file( "main.py" )

        await self.exit_bootloader()

        log.info("######################### RUN #########################")
        #
        #   Now we send commands in any order and don't check responses.
        #   Responses will be parsed in parse_status.
        #
        print("connect MQTT")
        self.mqtt = MQTTWrapper( "pv_mainboard" )
        self.mqtt_topic = "pv/"
        await self.mqtt.mqtt.connect( config.MQTT_BROKER_LOCAL )

        MQTTVariable( "nolog/pv/solis1/fan_speed", self, "solis1_fan", float, None, 0, self.on_mqtt_update_fan )
        MQTTVariable( "nolog/pv/solis2/fan_speed", self, "solis2_fan", float, None, 0, self.on_mqtt_update_fan )
        self.mqtt.subscribe_callback( "nolog/pv/event/solis1"   , self.on_mqtt_event_led( 0 ) )
        self.mqtt.subscribe_callback( "nolog/pv/event/ms1"      , self.on_mqtt_event_led( 3 ) )
        self.mqtt.subscribe_callback( "nolog/pv/event/solis2"   , self.on_mqtt_event_led( 1 ) )
        self.mqtt.subscribe_callback( "nolog/pv/event/ms2"      , self.on_mqtt_event_led( 4 ) )
        self.mqtt.subscribe_callback( "nolog/pv/event/meter"    , self.on_mqtt_event_led( 2 ) )
        self.mqtt.subscribe_callback( "nolog/pv/event/evse"     , self.on_mqtt_event_led( 5 ) )

        pv.reload.add_module_to_reload( "config", self.mqtt.load_rate_limit ) # reload rate limit configuration

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task( self.log_coroutine( "Read responses coroutine" , self.read_coroutine() ))
                tg.create_task( self.log_coroutine( "Poll status coroutine"    , self.poll_status_coroutine() ))
                tg.create_task( self.log_coroutine( "Sysinfo coroutine"        , self.sysinfo_coroutine() ))
                tg.create_task( self.log_coroutine( "Diskinfo coroutine"       , self.diskinfo_coroutine() ))
                tg.create_task( self.log_coroutine( "Reload python modules"    , pv.reload.reload_coroutine() ))

        except (KeyboardInterrupt, CancelledError):
            print("Terminated.")
        finally:
            await asyncio.sleep(0.5)    # wait for MQTT to finish publishing
            await self.mqtt.mqtt.disconnect()
            with open("mqtt_stats/pv_router.txt","w") as f:
                self.mqtt.write_stats( f )


    async def read_coroutine( self ):
        while True:
            try:
                await self.wait_ready() # parse status reports
            except Exception:
                log.exception( "Read coroutine")

    async def poll_status_coroutine( self ):
        tick = Metronome( 2 )
        n = 0
        while True:
            try:
                await tick.wait()
                await self.send("stat\n")
                # await self.send("flash %d\n" % (n,) )
                # n = (n+1)%9
            except Exception:
                log.exception( "Status coroutine:")

    ########################################################################################
    #   System info
    ########################################################################################
    async def sysinfo_coroutine( self ):
        prev_cpu_timings = None
        tick = Metronome( 1.0 )
        while True:
            await tick
            if not config.ENABLE_SYSINFO:
                continue
            with open("/proc/stat") as f:
                cpu_timings = [ int(_) for _ in f.readline().split()[1:] ]
                cpu_timings = cpu_timings[3], sum(cpu_timings)  # idle time, total time
                if prev_cpu_timings:
                    self.mqtt.publish_value( "pv/cpu_load_percent", round( 100.0*( 1.0-(cpu_timings[0]-prev_cpu_timings[0])/(cpu_timings[1]-prev_cpu_timings[1]) ), 1 ))
                prev_cpu_timings = cpu_timings

            await asyncio.sleep(0)
            with open("/sys/devices/virtual/thermal/thermal_zone0/temp") as f:
                self.mqtt.publish_value( "pv/cpu_temp_c", round( int(f.read())*0.001, 1 ) )

    ########################################################################################
    #   Disk info
    ########################################################################################
    async def diskinfo_coroutine( self ):
        prev_cpu_timings = None
        tick = Metronome( 60.0 )
        while True:
            await tick
            if not config.ENABLE_DISKINFO:
                continue
            total, used, free = shutil.disk_usage("/")
            self.mqtt.publish_value( "pv/disk_space_gb", round( free/2**30, 2 ) )


try:
    log.info("######################### START #########################")
    pico = PiPico()
    pico.start()
finally:
    logging.shutdown()



