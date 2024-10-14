#!/usr/bin/python
# -*- coding: utf-8 -*-

import asyncio, os, time, clickhouse_driver, datetime, math, traceback, subprocess, logging, sys, uvloop
import orjson, socket
import numpy as np
from gmqtt import Client as MQTTClient
from path import Path
from xopen import xopen
import config
from misc import *

#
#   This imports MQTT log files and inserts them into clickhouse
#   Use if the machine with clickhouse was down to catch up
#   on real time data.
#   Also launch it periodically to 
#
logging.basicConfig( #encoding='utf-8', 
                     level=logging.INFO,
                     format='[%(asctime)s] %(levelname)s:%(message)s',
                     handlers=[logging.FileHandler(filename=Path("/mnt/ssd/temp/log/")/Path(__file__).stem+'.log'), 
                            logging.StreamHandler(stream=sys.stdout)])
log = logging.getLogger(__name__)



#
#   After install clickhouse, reduce logging:
#   https://theorangeone.net/posts/calming-down-clickhouse/
#
"""

random queries

compute energy integral
create temporary table tt as select toStartOfHour(ts) h, v, (t-lagInFrame(t,1) over (order by ts)) dt from
            (select ts, toFloat64(ts) t, value v from mqtt_float where topic='pv/meter/total_power' and ts > '2023-02-10' order by topic,ts) a;
select h, sum(greatest(0,v)*dt)/3.6e6 from tt where dt<100 and h<'2023-02-11' group by h order by h;

create temporary table tt as select toStartOfDay(ts) h, v, (t-lagInFrame(t,1) over (order by ts)) dt from
            (select ts, toFloat64(ts) t, value v from mqtt_float where topic='chauffage/pompe' order by topic,ts) a;
select h, sum(dt)/3600 from tt where v=1 group by h order by h;



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


clickhouse = clickhouse_driver.Client('localhost', user=config.CLICKHOUSE_USER, 
    password=config.CLICKHOUSE_PASSWORD )

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
        topic   LowCardinality( String ) NOT NULL            CODEC(ZSTD(9)), 
        ts      DateTime64(2,'UTC') NOT NULL DEFAULT now()   CODEC(Delta,ZSTD(9)),   -- ts with 10 millisecond precision
        is_int  UInt8   NOT NULL                             CODEC(ZSTD(9)),         -- 0:float 1:int/bitfield
        value   Float64 NOT NULL                             CODEC(Delta,ZSTD(9)),
    )   ENGINE=ReplacingMergeTree -- eliminates duplicates with same (topic,ts) which should not occur
        ORDER BY (topic,ts)
        PRIMARY KEY (topic,ts);
""")

# Buffer table to reduce disk writes on inserts
clickhouse.execute( """
    CREATE TABLE IF NOT EXISTS mqtt_float( 
        topic   LowCardinality( String ) NOT NULL, 
        ts      DateTime64(2,'UTC') NOT NULL DEFAULT now(),   -- ts with 10 millisecond precision
        is_int  UInt8   NOT NULL,         -- 0:float 1:int/bitfield
        value   Float64 NOT NULL,
    ) ENGINE=Buffer(mqtt, mqtt_float_store, 1, 60, 120, 100, 1000, 65536, 1048576 );
""" )

# Store strings, logs, exceptions, etc
clickhouse.execute( """
    CREATE TABLE IF NOT EXISTS mqtt_str( 
        topic   LowCardinality( String ) NOT NULL       CODEC(ZSTD(9)),     
        ts      DateTime64(2,'UTC') NOT NULL DEFAULT now()  CODEC(Delta,ZSTD(9)),
        is_exception UInt8   NOT NULL                                      CODEC(ZSTD(9)),
        value   String NOT NULL                         CODEC(ZSTD(9)),
    )   ENGINE=ReplacingMergeTree
        ORDER BY (topic,ts)
        PRIMARY KEY (topic,ts);
""")

# Coalesce data by the minute to make graphing faster
# clickhouse.execute( """CREATE OR REPLACE VIEW mqtt_minute AS
# SELECT
#     topic,
#     toStartOfMinute(ts) AS ts,
#     max(is_int)         AS is_int,
#     avg(value)          AS favg,
#     min(value)          AS fmin,
#     max(value)          AS fmax,
#     groupBitAnd ( toUInt64(value) ) AS iand,
#     groupBitOr  ( toUInt64(value) ) AS ior    
# FROM mqtt_float
# WHERE NOT isNaN(value)
# GROUP BY topic,ts
# """ )

clickhouse.execute( """CREATE OR REPLACE VIEW mqtt_float_valid AS
SELECT *
FROM mqtt_float
WHERE NOT isNaN(value)
""" )

#
#   Queries for real time summaries with materialized views
#
"""
CREATE MATERIALIZED VIEW mqtt_minute_mat ENGINE = AggregatingMergeTree ORDER BY (topic,ts)
POPULATE
AS SELECT
    topic,
    toStartOfMinute(ts) AS ts,
    maxState(is_int)        AS is_int,
    avgState(value)         AS favg,
    minState(value)         AS fmin,
    maxState(value)         AS fmax
FROM mqtt_float_store
WHERE NOT isNaN(value)
GROUP BY topic,ts;

CREATE VIEW mqtt_minute AS SELECT
topic, ts,
       maxMerge(is_int)     AS is_int, 
       avgMerge(favg)       AS favg, 
       maxMerge(fmax)       AS fmax, 
       minMerge(fmin)       AS fmin
FROM mqtt_minute_mat
GROUP BY topic, ts;

CREATE MATERIALIZED VIEW mqtt_10minute_mat ENGINE = AggregatingMergeTree ORDER BY (topic,ts)
POPULATE
AS SELECT
    topic,
    toStartOfInterval(ts, INTERVAL 10 MINUTE) AS ts,
    maxState(is_int)        AS is_int,
    avgState(value)         AS favg,
    minState(value)         AS fmin,
    maxState(value)         AS fmax
FROM mqtt_float_store
WHERE NOT isNaN(value)
GROUP BY topic,ts;

CREATE VIEW mqtt_10minute AS SELECT
topic, ts,
       maxMerge(is_int)     AS is_int, 
       avgMerge(favg)       AS favg, 
       maxMerge(fmax)       AS fmax, 
       minMerge(fmin)       AS fmin
FROM mqtt_10minute_mat
GROUP BY topic, ts;

CREATE MATERIALIZED VIEW mqtt_100minute_mat ENGINE = AggregatingMergeTree ORDER BY (topic,ts)
POPULATE
AS SELECT
    topic,
    toStartOfInterval(ts, INTERVAL 100 MINUTE) AS ts,
    maxState(is_int)        AS is_int,
    avgState(value)         AS favg,
    minState(value)         AS fmin,
    maxState(value)         AS fmax
FROM mqtt_float_store
WHERE NOT isNaN(value)
GROUP BY topic,ts;

CREATE VIEW mqtt_100minute AS SELECT
topic, ts,
       maxMerge(is_int)     AS is_int, 
       avgMerge(favg)       AS favg, 
       maxMerge(fmax)       AS fmax, 
       minMerge(fmin)       AS fmin
FROM mqtt_100minute_mat
GROUP BY topic, ts;

CREATE MATERIALIZED VIEW mqtt_day_mat ENGINE = AggregatingMergeTree ORDER BY (topic,ts)
POPULATE
AS SELECT
    topic,
    toStartOfDay(ts) AS ts,
    maxState(is_int)        AS is_int,
    avgState(value)         AS favg,
    minState(value)         AS fmin,
    maxState(value)         AS fmax
FROM mqtt_float_store
WHERE NOT isNaN(value)
GROUP BY topic,ts;

CREATE VIEW mqtt_day AS SELECT
topic, ts,
       maxMerge(is_int)     AS is_int, 
       avgMerge(favg)       AS favg, 
       maxMerge(fmax)       AS fmax, 
       minMerge(fmin)       AS fmin
FROM mqtt_day_mat
GROUP BY topic, ts;

--
-- This table is guaranteed to contain a row every minute for every topic with no holes
--
CREATE TABLE IF NOT EXISTS mqtt_minute_filled( 
    topic   LowCardinality( String ) NOT NULL            CODEC(ZSTD(9)), 
    ts      DateTime64(0,'UTC') NOT NULL DEFAULT now()   CODEC(Delta,ZSTD(9)),   -- ts with second precision
    age     UInt32  NOT NULL                             CODEC(Delta,ZSTD(9)),   -- 0 if this is an original row, otherwise age in seconds of the copied row relative to current row
    favg    Float64 NOT NULL                             CODEC(Delta,ZSTD(9)),
    fmin    Float64 NOT NULL                             CODEC(Delta,ZSTD(9)),
    fmax    Float64 NOT NULL                             CODEC(Delta,ZSTD(9)),
)   ENGINE=ReplacingMergeTree -- eliminates duplicates with same (topic,ts) which should not occur
    ORDER BY (topic,ts)
    PRIMARY KEY (topic,ts);


select (SELECT max(ts) from mqtt_minute where topic='chauffage/relays/4') + INTERVAL number MINUTE from numbers(10);

"""


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


#
#   Gathers inserts into one large operation
#
class InsertPooler():
    table_float = "mqtt_float"
    table_str   = "mqtt_str"

    def __init__( self ):
        self.start_ts = None
        self.reset()
        self.begin_ts = time.time()
        self.insert_floats = []
        self.insert_str    = []
        self.last = {}

    def reset( self, l=0, st=0 ):
        t = time.time()
        if self.start_ts:
            interval = t-self.start_ts
            r = "%d rows %.02f rows/s" % (l,l/interval)
        else:
            r = ""
        self.start_ts = t
        return r


    def add( self, ts, k, v ):
        # clickhouse accepts numeric format, so don't bother with ISO timestamp.
        self._add( int(ts*100), k, v )

    def notdupe( self, ts, k, v ):
        last = self.last.get(k)
        if last:
            last_ts, last_v = last
            if last_v == v and ts < last_ts+1000:     # unit is 10ms
                return
        self.last[k] = (ts,v)
        return True

    def _add( self, ts, k, v ):
        if isinstance( v, int ):
            if self.notdupe( ts,k,v ):
                self.insert_floats.append((k,ts,1,v))
            return
        elif isinstance( v, float ):
            if math.isfinite(v):
                if self.notdupe( ts,k,v ):
                    self.insert_floats.append((k,ts,0,v))  
            return
        elif isinstance( v, (dict,list) ):
            pass
            # print("Ignore", k, v )
        else:
            try:
                # Try to convert to int, will fail if it's a float
                return self._add( ts, k, int(v) )
            except:
                try: # try float...
                    return self._add( ts, k, float(v) )
                except:
                    if k.endswith("/exception"):
                        if self.notdupe( ts,k,v ):
                            return self.insert_str.append((k,ts,True,v))
                    elif v.startswith("{"):
                        return self._add( ts, k, orjson.loads( v ))
                    else:
                        if self.notdupe( ts,k,v ):
                            return self.insert_str.append((k,ts,False,v))

    def flush( self ):
        st = time.time()
        l = len(self.insert_str)+len(self.insert_floats)
        try:
            if self.insert_floats:
                clickhouse.execute( "INSERT INTO %s (topic,ts,is_int,value) VALUES" % self.table_float, self.insert_floats )
                self.insert_floats = []
            if self.insert_str:
                clickhouse.execute( "INSERT INTO %s (topic,ts,is_exception,value) VALUES"%self.table_str, self.insert_str )
                self.insert_str    = []
        except Exception as e:
            print( "Lost connection to Clickhouse, %d rows pending" % l )
        else:
            r = self.reset( l, st )
            print( "INSERT %f ms %s" % ((time.time()-st)*1000, r ))


async def cleanup_coroutine( ):
    while True:
        try:
            for table in "system.trace_log", "system.metric_log", "system.asynchronous_metric_log", "system.session_log":
                print("Cleanup", table, clickhouse.execute("SELECT count(*) FROM "+table ))
                clickhouse.execute( "TRUNCATE TABLE "+table )
                await asyncio.sleep(20)
        except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
            return abort()
        except:
            log.error( "Exception: %s", traceback.format_exc() )
            await asyncio.sleep(10)

async def database_size_coroutine( mqtt ):
    while True:
        try:
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
        except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
            return abort()
        except:
            log.error( "Exception: %s", traceback.format_exc() )
            await asyncio.sleep(10)


async def astart():
    mqtt = MQTTClient("clickhouse_logger_v2")
    mqtt.set_auth_credentials( config.MQTT_USER,config.MQTT_PASSWORD )
    await mqtt.connect( config.MQTT_BROKER )

    asyncio.create_task( database_size_coroutine( mqtt ) )
    asyncio.create_task( cleanup_coroutine() )

    while True:
        try:
            await transfer_data( mqtt )
        except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
            return abort()
        except:
            log.error( "Exception: %s", traceback.format_exc() )
            await asyncio.sleep(1)

async def transfer_data( mqtt ):
    # setup local
    tmp_dir = Path( config.MQTT_BUFFER_TEMP )
    tmp_dir.makedirs_p()

    r = clickhouse.execute( "SELECT toUInt64(max(ts)) FROM mqtt_float ")
    start_timestamp = r[0][0] - RETRIEVE_SECONDS
    # start_timestamp = 1691942400
    log.info("Start timestamp: %d", start_timestamp)

    # connect to log server
    log.info("Connecting to log server")
    rsock,wsock = await asyncio.open_connection( config.MQTT_BUFFER_IP, config.MQTT_BUFFER_PORT )
    wsock.write(b"%d\n" % start_timestamp)        # send timestamp

    pool = InsertPooler()
    st = time.time()
    length2 = 0.
    total_rows = 0
    while True:
        line = (await rsock.readline()).split()
        file_ts = float(line[0])
        length  = int(line[1])            
        log.debug("Got length:%d", length)
        if length == 0:
            continue
        if length > 0:
            #   We could stream decompress... but if there's an error, we'll have to
            #   make sure we read up to the next chunk, too complicated!
            #   just use temp file
            try:
                with open( tmp_path := tmp_dir/("%.02f.json.zst"%file_ts), "wb" ) as zf:
                    length2 += length/1024
                    while length>0:
                        data = await rsock.read( min(length,65536) )
                        zf.write( data )
                        length -= len( data )
                log.info( "File at %s: Recv: %6d kB, %6d kB/s, %d rows", 
                    datetime.datetime.fromtimestamp(file_ts).isoformat(), 
                    length2, length2/(time.time()-st), total_rows )
                try:
                    for n,line in enumerate( xopen( tmp_path ) ):
                        j = orjson.loads( line )
                        total_rows += 1
                        if j[0] > start_timestamp:
                            try:
                                pool.add( *j )
                            except Exception as e:
                                print("Error on line %s: %s\n> %s" % (n, e, line[:80]))                        
                            if not (n&0x3FFFF):
                                pool.flush()
                except Exception as e:
                    print("Error on file %s: %s" % (tmp_path, e))
            finally:
                pass
                # tmp_path.unlink()
        else:
            #   Get real time data
            #
            pool.flush()
            timer = Metronome( config.CLICKHOUSE_INSERT_PERIOD_SECONDS )
            while True:
                async with asyncio.timeout( 60 ):
                    line = await rsock.readline()
                if not line:
                    return
                try:
                    pool.add( *orjson.loads( line ) )
                except Exception as e:
                    log.exception( "Error" )

                # print( len( pool.insert_floats ))
                if timer.ticked():
                    pool.flush()


RETRIEVE_SECONDS = 4*3600

# # unpack json values from tasmota plugs into individual rows
# pool = InsertPooler()
# for n, (topic, ts, value) in enumerate( clickhouse.execute( "SELECT topic, ts, value FROM mqtt_str WHERE value LIKE '{%'") ):
#     # print( topic, ts, value )
#     pool._add( ts, topic, value )
#     if not (n&0x3FFF):
#         pool.flush()
# pool.flush()
# stop

if sys.version_info >= (3, 11):
    if len( sys.argv ) > 1:
        try:
            RETRIEVE_SECONDS = int( sys.argv[1] )*3600
        except:
            print("usage: %s [n]\n  n number of days worth of data to retrieve from MQTT logger")
            sys.exit(1)
    log.info("######################### START #########################")
    with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
        runner.run(astart())
else:
    uvloop.install()
    asyncio.run(astart())





