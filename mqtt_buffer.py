#!/usr/bin/python
# -*- coding: utf-8 -*-

import asyncio, os, time, traceback, collections, orjson, zstandard, logging, sys, datetime, uvloop, signal
import collections
from xopen import xopen
from path import Path

import config
from misc import *
from pv.mqtt_wrapper import MQTTWrapper

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

class Buffer( MQTTWrapper ):
    def __init__( self, basedir ):
        super().__init__( "mqtt_buffer" )

        # MQTT -> thread deque
        self.queue_socket = collections.deque( maxlen=65536 )
        self.msgcount = 0
        self.msgcount_tick = Metronome( 10 )

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

    def __enter__(self):
        self.file_new()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.file_close()

    ########################################################
    #   Housekeeping
    ########################################################
    def start( self ):
        with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
            runner.run(self.astart())

    async def astart( self ):
        server = await asyncio.start_server( self.handle_client, config.MQTT_BUFFER_IP, config.MQTT_BUFFER_PORT )
        await self.mqtt.connect( config.MQTT_BROKER_LOCAL )
        await server.serve_forever()

    ########################################################
    #   MQTT
    ########################################################

    def on_connect( self, client, flags, rc, properties ):
        self.mqtt.subscribe( "#" )
        self.mqtt.subscribe( "$SYS/broker/load/messages/received/1min" )
        self.mqtt.subscribe( "$SYS/broker/load/messages/sent/1min" )
        self.mqtt.subscribe( "$SYS/broker/load/bytes/received/1min" )
        self.mqtt.subscribe( "$SYS/broker/load/bytes/sent/1min" )


    async def on_message( self, client, topic, payload, qos, properties ):
        # Do not store high traffic control messages, for example
        # fakemeter updates
        if topic.startswith("nolog/"):
            return

        # Encode MQTT message into json
        jl = orjson.dumps( [ round(time.time(),2), topic, payload.decode() ], option=orjson.OPT_APPEND_NEWLINE )
        self.msgcount += 1

        # Queue it. deque() with maxlen is a ring buffer and will discard oldest items when full.
        self.queue_socket.append( jl )
        self.compressor.write( jl )
        if self.flush_tick.ticked():
            self.compressor.flush()
        if self.new_file_tick.ticked():
            self.file_new()
        if elapsed := self.msgcount_tick.ticked():
            print("Queue %d ; %f messages/s" % (len(self.queue_socket), self.msgcount/elapsed))
            self.msgcount = 0


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

        try:
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
                while True:
                    while self.queue_socket:
                        writer.write( self.queue_socket.popleft() )
                    await writer.drain()
                    await asyncio.sleep(0.1)
        except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
            raise
        except:
            log.exception('Exception')
            raise




if __name__ == '__main__':
    try:
        log.info("MQTT logger starting.")
        with Buffer( config.MQTT_BUFFER_PATH ) as buf:
            buf.start()
    finally:
        log.info("MQTT logger stopping.")
        logging.shutdown()

# gmqtt also compatibility with uvloop  
# import uvloop
# asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
