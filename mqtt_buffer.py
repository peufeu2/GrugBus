#!/usr/bin/python
# -*- coding: utf-8 -*-

import asyncio, os, time, traceback, collections, orjson, zstandard, logging, sys, datetime
from path import Path
from gmqtt import Client as MQTTClient
import config, grugbus

"""
    This logs all MQTT traffic into zstd compressed files stored in MQTT_BUFFER_PATH.
    A new file is created every hour, or if topic sys/buffer/newfile is published to.

    The database logger can come and grab files via sshfs, then delete them.

    This is a crude and simple way to make sure data is kept if the database is down.
"""


logging.basicConfig( encoding='utf-8', 
                     level=logging.INFO,
                     format='[%(asctime)s] %(levelname)s:%(message)s',
                     handlers=[logging.FileHandler(filename='mqtt_buffer.log'), 
                            logging.StreamHandler(stream=sys.stdout)])
log = logging.getLogger(__name__)

STOP = asyncio.Event()
def ask_exit(*args):
    STOP.set()

class Buffer():
    def __init__( self, basedir ):
        self.mqtt = MQTTClient("buffer")
        self.mqtt.on_connect    = self.on_connect
        self.mqtt.on_message    = self.on_message
        # self.mqtt.on_disconnect = self.on_disconnect
        # self.mqtt.on_subscribe  = self.on_subscribe
        self.mqtt.set_auth_credentials( config.MQTT_USER, config.MQTT_PASSWORD )
        self.published_data = {}
        self.basedir = Path( basedir )
        self.curfile = None
        self.flush_timeout = grugbus.Timeout( 60 )
        self.new_file_timeout = grugbus.Timeout( 3600 )
        self.force_new_file_timeout = grugbus.Timeout( 3, expired=True )
        # compression levels 
        #   10 uses 30MB RAM compress ratio 10.88x
        #    7       9MB RAM                10.16x  <- best compromise
        #    5       6MB RAM                 8.57x 
        self.cctx = zstandard.ZstdCompressor( level=7 )
        self.file_new()

    def file_close( self ):
        if self.curfile:
            self.compressor.close()
            log.info( "Close %s - %s", self.curfname, self.get_ratio() )
            self.curfile.close()
            self.curfname.rename( self.curfname )   # remove .tmp
            self.curfile = self.compressor = None

    def file_new( self ):
        self.file_close()
        self.curfname = self.basedir / ("%s.json.zst" % datetime.datetime.now().isoformat().replace(":","-") )
        log.info( "New file %s", self.curfname )
        self.curfile = open( self.curfname+".tmp","wb" )
        self.compressor = self.cctx.stream_writer( self.curfile )
        self.flush_timeout.reset()
        self.new_file_timeout.reset()

    def on_connect(self, client, flags, rc, properties):
        self.mqtt.subscribe('#', qos=0)

    # def on_disconnect(self, client, packet, exc=None):
        # pass

    def get_ratio( self ):
        bytes_in,bytes_consumed,bytes_out = self.cctx.frame_progression()
        if bytes_out:
            return "%d->%d %.02fx %.02fMB" % (bytes_in,bytes_out,bytes_in/bytes_out,self.cctx.memory_size()/1048576)
        else:
            return "no bytes written"

    async def on_message(self, client, topic, payload, qos, properties):
        self.compressor.write( orjson.dumps( [ round(time.time(),2), topic, payload.decode() ], option=orjson.OPT_APPEND_NEWLINE ) )
        if self.flush_timeout.expired():
            self.flush_timeout.reset()
            self.compressor.flush()
        if self.new_file_timeout.expired():
            self.file_new()
        elif topic == "sys/buffer/newfile" and self.force_new_file_timeout.expired() and payload==b"confirm":
            self.force_new_file_timeout.reset() # limit rate of new file creation
            self.file_new()


    # compressor.flush()


async def main( ):
    log.info("MQTT logger starting.")
    buf = Buffer( config.MQTT_BUFFER_PATH )
    # while True:
    #     try:
    #         await buf.mqtt.connect( config.MQTT_BROKER )
    #     except Exception as e:
    #         log.error( "Connect: %s", e )
    #         await asyncio.sleep(1)
    #     else:
    #         log.info( "MQTT Connected" )
    #         break
    try:
        # this will wait until network is up and connect
        asyncio.create_task( buf.mqtt.connect( config.MQTT_BROKER_LOCAL ) )
        await STOP.wait()
    except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
        log.info( "Terminating..." )
    except:
        log.error( "Exception: %s", traceback.format_exc() )
    finally:
        buf.file_close()
        await buf.mqtt.disconnect()

if __name__ == '__main__':
    asyncio.run(main( ))


# gmqtt also compatibility with uvloop  
# import uvloop
# asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
