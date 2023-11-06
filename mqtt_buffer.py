#!/usr/bin/python
# -*- coding: utf-8 -*-

import asyncio, os, time, traceback, collections, orjson, zstandard, logging, sys, datetime, uvloop, signal
import collections
from xopen import xopen
from path import Path
from gmqtt import Client as MQTTClient
import config
from misc import *

"""
    To log MQTT traffic into a database, we need a computer that runs a database.
    This tiny Pi is way too slow for that, so let's run the database on something else.
    However the database can be down and we want to keep logging.

    So we will store MQTT logs locally.

    To merge them with logs already in the database, we need TIMESTAMPS to match,
    ==> THIS PI WILL BE THE SOLE SOURCE OF TIMESTAMPS <==

    So the PC with the database can't connect using MQTT, as it wouldn't have the timestamps!

    This program grabs all MQTT traffic, adds a timestamp, and encodes it into JSON-lines.
    Everything is stored into zstd compressed files stored in MQTT_BUFFER_PATH.
    A new file is created every hour, or MQTT_BUFFER_FILE_DURATION seconds.

    It also offers a server socket, which supports only one client at a time.
    Connecting to this allows pulling log files and receiving data in real time.
"""


logging.basicConfig( encoding='utf-8', 
                     level=logging.INFO,
                     format='[%(asctime)s] %(levelname)s:%(message)s',
                     handlers=[logging.FileHandler(filename=Path(__file__).stem+'.log'), 
                            logging.StreamHandler(stream=sys.stdout)])
log = logging.getLogger(__name__)

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


class Buffer():
    def __init__( self, basedir ):

        # Init MQTT
        self.mqtt = MQTTClient("buffer2")
        self.mqtt.on_connect    = self.on_connect
        self.mqtt.on_message    = self.on_message
        self.mqtt.set_auth_credentials( config.MQTT_USER, config.MQTT_PASSWORD )

        # MQTT -> thread deque
        self.queue_socket = collections.deque( maxlen=65536 )

        # Logging to files
        self.basedir = Path( basedir )
        self.all_files = collections.deque( self.get_existing_files() )
        self.files_to_send = collections.deque()
        self.curfile = None
        self.flush_tick = Metronome( 120 )
        self.new_file_tick = Metronome( config.MQTT_BUFFER_FILE_DURATION )
        # compression levels 
        #   10 uses 30MB RAM compress ratio 10.88x
        #    7       9MB RAM                10.16x  <- best compromise
        #    5       6MB RAM                 8.57x 
        self.cctx = zstandard.ZstdCompressor( level=7 )
        self.file_new()

    ########################################################
    #   Housekeeping
    ########################################################
    def start( self ):
        if sys.version_info >= (3, 11):
            with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
            # with asyncio.Runner() as runner:
                runner.run(self.astart())
        else:
            uvloop.install()
            asyncio.run(self.astart())

    async def astart( self ):
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGINT,  abort)
        loop.add_signal_handler(signal.SIGTERM, abort)

        try:
            # This blocks until connected (or fails if mosquitto is down).
            # If it connects once, it will reconnect automatically.
            await self.mqtt.connect( config.MQTT_BROKER_LOCAL ) 

            server = await asyncio.start_server( 
                self.handle_client, config.MQTT_BUFFER_IP, config.MQTT_BUFFER_PORT )
            asyncio.create_task( server.serve_forever() )
            await STOP.wait()
        except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
            log.info( "Terminating..." )
        except:
            log.error( "Exception: %s", traceback.format_exc() )
        finally:
            self.file_close()
            await self.mqtt.disconnect()


    ########################################################
    #   MQTT
    ########################################################
    def on_connect(self, client, flags, rc, properties):
        self.mqtt.subscribe('#', qos=0)

    async def on_message(self, client, topic, payload, qos, properties):
        # Encode MQTT message into json
        jl = orjson.dumps( [ round(time.time(),2), topic, payload.decode() ], option=orjson.OPT_APPEND_NEWLINE )
        # Queue it. deque() with maxlen is a ring buffer and will discard oldest items when full.
        self.queue_socket.append( jl )
        self.compressor.write( jl )
        if self.flush_tick.ticked():
            self.compressor.flush()
        if self.new_file_tick.ticked():
            self.file_new()

    ########################################################
    #   Log storage
    ########################################################
    def file_new( self ):
        # compression levels 
        #   10 uses 30MB RAM compress ratio 10.88x
        #    7       9MB RAM                10.16x  <- best compromise
        #    5       6MB RAM                 8.57x 
        self.clean_old_files()
        self.file_close()
        fname = self.basedir / ("%.02f.json.zst" % time.time() )
        self.all_files.append( fname )
        self.files_to_send.append( fname ) # if transfer is in progress, also queue it
        log.info( "New file %s", fname )
        self.curfile = open( fname, "wb" )
        self.compressor = self.cctx.stream_writer( self.curfile )

    def file_close( self ):
        if self.curfile:
            self.compressor.flush()
            self.compressor.close()
            log.info( "Close %s - %s", self.curfile.name.stem, self.get_ratio() )
            self.curfile.close()
            self.curfile = None

    def get_ratio( self ):
        bytes_in,bytes_consumed,bytes_out = self.cctx.frame_progression()
        if bytes_out:
            return "Compress %d->%d %.02fx RAM %.02fMB" % (
                bytes_in,bytes_out,bytes_in/bytes_out,self.cctx.memory_size()/1048576)
        else:
            return "no bytes written"

    def fname_to_timestamp( self, fname ):
        return float(fname.stem.split(".json")[0])

    def clean_old_files( self, retention_delay=config.MQTT_BUFFER_RETENTION ):
        cutoff = time.time() - retention_delay
        while self.all_files:
            fname = self.all_files[0]
            if self.fname_to_timestamp(fname) < cutoff:
                log.info( "Deleting old file %s", fname )
                self.all_files.popleft()
                if fname.exists():
                    fname.unlink()
            else:
                break

    def get_existing_files( self ):
        r = []
        for fname in self.basedir.glob("*.zst"):
            if not fname.size:
                log.info( "Deleting empty file %s", fname )
                fname.unlink()
                continue
            try:
                ts = self.fname_to_timestamp( fname )
            except ValueError:
                log.info( "Deleting invalid file %s", fname )
                fname.unlink()
                continue
            r.append( fname )
        return sorted( r, key=self.fname_to_timestamp )

    ########################################################
    #   Server
    ########################################################
    async def handle_client( self, reader, writer ):
        async def sendfile( fname ):
            log.info( "Sendfile %s q %d", fname.stem, len(self.queue_socket) )
            writer.write( b"%f %d\n" % (self.fname_to_timestamp( fname ), fname.size ))
            with open( fname, "rb" ) as f:
                while data := f.read(65536):
                    writer.write(data)
                    await writer.drain()
                    await asyncio.sleep( 0 )

        log.info("Client connected")
        async for line in reader:
            # This server has exactly one command: gimme data from specified timestamp.
            start_ts = float( line )-config.MQTT_BUFFER_FILE_DURATION*2-10
            log.info("Client: start_ts %s", start_ts)

            # we will consume this list
            self.files_to_send = collections.deque(self.all_files)

            # Send all files past requested timestamp
            # if file_new() adds another one while we're sending, it
            # will also be queued
            while self.files_to_send:
                await asyncio.sleep( 0 )
                fname = self.files_to_send.popleft()
                if self.fname_to_timestamp( fname ) > start_ts:
                    if fname != self.curfile.name:
                        await sendfile( fname )
                    else:
                        log.info("Send last file")
                        self.queue_socket.clear()   # clear real time queue
                        self.file_new()             # stop writing to file before sending it
                        await sendfile( fname )
                        # if real time queue didn't fill up while we were sending the file,
                        # then we didn't miss any records, we can move to the next step
                        # otherwise the records are in the file, so we loop again
                        if len(self.queue_socket)<self.queue_socket.maxlen:
                            break

            # Now forward real time data
            log.info("Send real time, queue %d", len(self.queue_socket))
            await writer.drain()
            writer.write( b"-1 -1\n" )
            timer = Metronome(2)
            while True:
                if timer.ticked():
                    print("Queue %d" % len(self.queue_socket))
                while self.queue_socket:
                    writer.write( self.queue_socket.popleft() )
                await writer.drain()
                await asyncio.sleep(0.1)




if __name__ == '__main__':
    try:
        log.info("MQTT logger starting.")
        buf = Buffer( config.MQTT_BUFFER_PATH )
        buf.start()
    finally:
        try:
            buf.file_close()
        finally:
            logging.shutdown()

# gmqtt also compatibility with uvloop  
# import uvloop
# asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
