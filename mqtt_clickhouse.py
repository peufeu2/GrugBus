#!/usr/bin/python
# -*- coding: utf-8 -*-

import asyncio, os, time, clickhouse_driver, datetime, pprint, math
from gmqtt import Client as MQTTClient
import config


#
#   After install clickhouse, reduce logging:
#   https://theorangeone.net/posts/calming-down-clickhouse/
#
"""

random queries

INSERT INTO mqtt_float (topic,ts,is_int,value)
SELECT 'pv/total_pv_power', s.ts, 0, m.value-s.f AS Power
FROM (
    SELECT ts, 'pv/solis1/pv_power' ntopic, value f
    FROM mqtt.mqtt_float 
    WHERE topic = 'pv/fronius/grid_port_power'
    AND ts < '2023-01-21 14:10:07.89'
    ) s
ASOF JOIN mqtt.mqtt_float AS m ON (m.topic=s.ntopic AND m.ts>=s.ts);

                self.publish( "pv/solis1/meter/", pub )

insert into mqtt_float (topic,ts,value) 
select 'pv/meter/house_power',ts,m.value-s.pv_export
FROM (SELECT ts, 'pv/meter/total_power' ntopic, value pv_export FROM mqtt.mqtt_float WHERE topic = 'pv/solis1/meter/active_power') s
ASOF JOIN mqtt.mqtt_float AS m ON (m.topic=s.ntopic AND m.ts>=s.ts)
WHERE s.ts < '2023-01-18 13:27:49.803';

insert into mqtt_float (topic,ts,value) 
select 'pv/solis1/mppt2_power', ts,m.value*s.value
FROM (SELECT ts, 'pv/solis1/mppt2_current' ntopic, value FROM mqtt.mqtt_float WHERE topic = 'pv/solis1/mppt2_voltage') s
ASOF JOIN mqtt.mqtt_float AS m ON (m.topic=s.ntopic AND m.ts>=s.ts)
WHERE s.ts < '2023-01-18 13:27:49.866';

"""


clickhouse = clickhouse_driver.Client('localhost', user=config.CLICKHOUSE_USER, password=config.CLICKHOUSE_PASSWORD )

for line in """USE mqtt;
SET log_queries=0;
SET log_query_threads=0;
SET log_query_views=0;
SET query_profiler_real_time_period_ns=0;
SET query_profiler_cpu_time_period_ns=0;
SET memory_profiler_step=0;
SET trace_profile_events=0;
SET log_queries_probability=0;
SET wait_for_async_insert=0;""".split("\n"):
    clickhouse.execute( line )

#
#   Setup clickhouse database
#
#   Data codecs selected for best compression on time series data (~ 10x)
#
#   One table for numeric data, and one for string/JSON data.
#   Numeric value is always stored as float, which is lossless for integers up to mantissa size.
#   is_int column is guessed from MQTT data: set to 0 if it is formatted as "1.0", set to 1 if there is no decimal point.
#   if is_int is false, it means notions of "average","min","max" make sense
#   otherwise it's probably a bitfield, flags, settings, etc, where min/max/avg makes no sense, but we may be
#   interested in bit operations, like "is the fault flag bit set?" which make no sense for floats.
#
#   is_int is constant for a topic, and clickhouse orders data by topic before compressing, so it will compresses to nothing.
#

# Main storage table for floats and ints
clickhouse.execute( """
    CREATE TABLE IF NOT EXISTS mqtt_float_store( 
        topic   LowCardinality( String ) NOT NULL                     CODEC(ZSTD(9)), 
        ts      DateTime64(2,'Europe/Paris') NOT NULL DEFAULT now()   CODEC(Delta,ZSTD(9)),   -- ts with millisecond precision
        is_int  UInt8   NOT NULL                                      CODEC(ZSTD(9)),         -- 0:float 1:int/bitfield
        value   Float64 NOT NULL                                      CODEC(Delta,ZSTD(9)),
    )   ENGINE=ReplacingMergeTree -- eliminates duplicates with same (topic,ts) which should not occur
        ORDER BY (topic,ts)
        PRIMARY KEY (topic,ts);
""")

# Buffer table to reduce disk writes on inserts
clickhouse.execute( """
    CREATE TABLE IF NOT EXISTS mqtt_float( 
        topic   LowCardinality( String ) NOT NULL, 
        ts      DateTime64(2,'Europe/Paris') NOT NULL DEFAULT now(),   -- ts with millisecond precision
        is_int  UInt8   NOT NULL,         -- 0:float 1:int/bitfield
        value   Float64 NOT NULL,
    ) ENGINE=Buffer(mqtt, mqtt_float_store, 1, 60, 120, 100, 1000, 65536, 1048576 );
""" )

# Store strings, logs, exceptions, etc
clickhouse.execute( """
    CREATE TABLE IF NOT EXISTS mqtt_str( 
        topic   LowCardinality( String ) NOT NULL       CODEC(ZSTD(9)),     
        ts      DateTime64(2,'Europe/Paris') NOT NULL DEFAULT now()  CODEC(Delta,ZSTD(9)),
        is_exception UInt8   NOT NULL                                      CODEC(ZSTD(9)),
        value   String NOT NULL                         CODEC(ZSTD(9)),
    )   ENGINE=ReplacingMergeTree
        ORDER BY (topic,ts)
        PRIMARY KEY (topic,ts);
""")

# Coalesce data by the minute to make graphing faster
clickhouse.execute( """CREATE OR REPLACE VIEW mqtt_minute AS
SELECT
    topic,
    toStartOfMinute(ts) AS ts,
    max(is_int),
    avg(value)          AS favg,
    min(value)          AS fmin,
    max(value)          AS fmax,
    groupBitAnd ( toUInt64(value) ) AS iand,
    groupBitOr  ( toUInt64(value) ) AS ior    
FROM mqtt_float
GROUP BY topic,ts
""" )

print("run")
"""
SELECT
table, name, compression_codec,
sum(data_uncompressed_bytes) uncompressed, 
sum(data_compressed_bytes) compressed, 
round(uncompressed/compressed,1) ratio
from system.columns 
group by table, name, compression_codec
having uncompressed != 0
order by table, name;
"""

#
#   Accumulate data until we have enough rows then insert
#
class DB():
    def __init__( self ):
        self.start_ts = None
        self.reset()
        self.begin_ts = time.time()
        self.topic_stats = {}

    def reset( self, st=0 ):
        t = time.time()
        if self.start_ts:
            interval = t-self.start_ts
            r = "%.02f rows/s" % (self.lines/interval)
        else:
            r = ""
        self.start_ts = t
        self.insert_floats = []
        self.insert_str    = []
        self.insert_timeout = st + config.CLICKHOUSE_INSERT_PERIOD_SECONDS
        self.lines = 0
        return r

    def insert( self, k, v ):
        v = v.decode( "utf-8" )
        st = time.time()
        ts = int(st*100)
        try:
            # Try to convert to int, will fail if it's a float
            self.insert_floats.append((k,ts,1,int(v)))                
        except:
            try:
                # try float...
                f=float(v)
                if math.isfinite(f):
                    self.insert_floats.append((k,ts,0,f))  
                else:
                    return
            except:
                # it's a string then
                self.insert_str.append((k,ts,k.endswith("/exception"),v))
        self.topic_stats[k] = self.topic_stats.get(k,0) + 1
        self.lines += 1
        if self.lines >= 1000 or st > self.insert_timeout:
            if self.insert_floats:
                self.insert_floats = sorted( self.insert_floats )
                # pprint.pprint( self.insert_floats )
                clickhouse.execute( "INSERT INTO mqtt_float (topic,ts,is_int,value) VALUES", self.insert_floats )
            if self.insert_str:
                clickhouse.execute( "INSERT INTO mqtt_str (topic,ts,is_exception,value) VALUES", self.insert_str )
            r = self.reset( st )
            print( "INSERT %f ms %s" % ((time.time()-st)*1000, r ))
            # for k,v in sorted( self.topic_stats.items(), key=lambda x:x[1] ):
            #     f = v/(st-self.begin_ts)
            #     if f > 0.1:
            #         print( "%4.1f/s %s" % (f,k))
            

db = DB()

#
#   Subscribe to everything that should be logged to database
#
def on_connect(mqtt, flags, rc, properties):
    print('Connected')
    mqtt.subscribe('pv/#', qos=0)
    mqtt.subscribe('sys/#', qos=0)

#
#   Get a message and log it
#
def on_message(mqtt, topic, payload, qos, properties):
    # print("%30s %s" % (time.time(), payload))
    db.insert( topic, payload )

def on_disconnect(mqtt, packet, exc=None):
    print('Disconnected')

def on_subscribe(mqtt, mid, qos, properties):
    print('SUBSCRIBED')

#
#   log database size once in a while
#
# async def database_rollup( mqtt ):
#     tick = grubgus.Metronome( 60 )
#     rows = clickhouse.execute( "SELECT max(ts) FROM mqtt_minute_float" )
#     if not rows:
#         last_ts = rows[0][0]
#     else:
#         last_ts = None

#     while True:
#         if last_ts:
#             # we processed up to the minute starting at last_ts, so add a minute
#             last_ts += datetime.timedelta( seconds=60 )
#             cur_ts = clickhouse.execute( "SELECT toStartOfMinute(now())")
#             print( last_ts )
#             clickhouse.execute( """
#                 INSERT INTO mqtt_minute_float (topic,ts,favg,fmin,fmax)
#                 SELECT topic, toStartOfMinute(ts) ts_min, avg(value), max(value), min(value) 
#                 FROM mqtt_float
#                 WHERE ts >= %s AND ts < toStartOfMinute(now())
#                 GROUP BY topic, ts_min
#                 """, (last_ts,) )

#         await tick.wait()

#
#   log database size once in a while
#
async def database_size( mqtt ):
    while True:
        r = clickhouse.execute( """
    select
    parts.*,
    columns.compressed_size,
    columns.uncompressed_size,
    columns.ratio
from (
    select database,
        table,
        (sum(data_uncompressed_bytes))          AS uncompressed_size,
        (sum(data_compressed_bytes))            AS compressed_size,
        sum(data_compressed_bytes) / sum(data_uncompressed_bytes) AS ratio
    from system.columns
    group by database, table
) columns right join (
    select database,
           table,
           sum(rows)                                            as rows,
           max(modification_time)                               as latest_modification,
           (sum(bytes))                       as disk_size,
           (sum(primary_key_bytes_in_memory)) as primary_keys_size,
           any(engine)                                          as engine,
           sum(bytes)                                           as bytes_size
    from system.parts
    where table like 'mqtt_%'
    group by database, table
) parts on ( columns.database = parts.database and columns.table = parts.table )
order by parts.bytes_size desc;
""")
        for database, table, rows, modtime, disk_size, primary_keys_size, engine, bytes_size, compressed_size, uncompressed_size, ratio in r:
            prefix = 'sys/db/%s/%s/' % (database,table)
            print(prefix)
            mqtt.publish( prefix + "rows"               , str(rows)             , qos=0 )
            mqtt.publish( prefix + "compressed_size"    , str(compressed_size)  , qos=0 )
            mqtt.publish( prefix + "uncompressed_size"  , str(uncompressed_size), qos=0 )
            if compressed_size:
                mqtt.publish( prefix + "compression_ratio"  , str(uncompressed_size/compressed_size), qos=0 )

        await asyncio.sleep( 3600 )


async def cleanup():
    while True:
        for table in "system.trace_log", "system.metric_log", "system.asynchronous_metric_log", "system.session_log":
            print("Cleanup", table, clickhouse.execute("SELECT count(*) FROM "+table ))
            clickhouse.execute( "TRUNCATE TABLE "+table )
            await asyncio.sleep(20)

#
#   event for exiting (we run forever)
#
STOP = asyncio.Event()
def ask_exit(*args):
    STOP.set()

#
#   connect to broker and start
#
async def main( ):
    mqtt = MQTTClient("clickhouse_logger")

    mqtt.on_connect = on_connect
    mqtt.on_message = on_message
    mqtt.on_disconnect = on_disconnect
    mqtt.on_subscribe = on_subscribe

    mqtt.set_auth_credentials( config.MQTT_USER,config.MQTT_PASSWORD )
    await mqtt.connect( config.MQTT_BROKER )

    asyncio.create_task( database_size( mqtt ) )
    asyncio.create_task( cleanup( ) )

    await STOP.wait()
    await mqtt.disconnect()

if __name__ == '__main__':
    asyncio.run(main( ))


# gmqtt also compatibility with uvloop  
# import uvloop
# asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
