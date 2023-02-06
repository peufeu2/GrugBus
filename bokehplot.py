import random, time, clickhouse_driver, collections
import asyncio, time, traceback, logging, sys, datetime
import numpy as np

from tornado.ioloop import IOLoop
import bokeh.events
from bokeh.server.server import Server
from bokeh.application import Application
from bokeh.application.handlers.function import FunctionHandler
from bokeh.plotting import figure, ColumnDataSource, figure, curdoc

from gmqtt import Client as MQTTClient

import config
from misc import *

PLOT_LENGTH = 200    # seconds

clickhouse = clickhouse_driver.Client('localhost', user=config.CLICKHOUSE_USER, password=config.CLICKHOUSE_PASSWORD )

def get_one( topic ):
    # current value
    r = clickhouse.execute( "SELECT value FROM mqtt.mqtt_float WHERE topic=%(topic)s ORDER BY ts DESC LIMIT 1", {"topic":topic} )
    # first value of day
    b = clickhouse.execute( "SELECT value FROM mqtt.mqtt_float WHERE topic=%(topic)s AND ts < toStartOfDay(now()) ORDER BY ts DESC LIMIT 1", {"topic":topic} )
    if r and b:
        return [b[0][0], r[0][0]]
    else:
        return [0,0]

class DataStream( object ):
    def __init__( self, topic, label, color, scale ):
        self.topic = topic
        self.label = label
        self.color = color
        self.scale = scale
        x,y = self.get( time.time()-PLOT_LENGTH )
        self.x = collections.deque( x )
        self.y = collections.deque( y )

    def get( self, tstart, tend=None, lod_length=3600 ):
        # All times in database are in UTC. Clickhouse treats integer timestamps as UTC too,
        # and will accept DateTime between (UTC int timestamp) and (UTC int timestamp)
        if isinstance( tstart, datetime.datetime ):
            tstart = tstart.timestamp()
            if tend:
                tstart = tend.timestamp()
        span = (tend or time.time())-tstart
        assert span>=0

        # dynamic level of detail
        lod = int(round( span/lod_length ) or 1)

        args   = { "topic":self.topic, "tstart":tstart, "tend":tend, "lod":lod, "lodm":lod//60 }
        kwargs = { "columnar":True, "settings":{"use_numpy":True} }            
        if tend:    ts_cond = "ts BETWEEN %(tstart)s AND %(tend)s"
        else:       ts_cond = "ts > %(tstart)s"
        where = " WHERE topic=%(topic)s AND " + ts_cond + " "

        t=time.time()
        if lod == 1:
            r = clickhouse.execute( "SELECT ts,value FROM mqtt.mqtt_float"+where+"ORDER BY topic,ts", args, **kwargs )
        elif lod < 30:
            r = clickhouse.execute( "SELECT toStartOfInterval(ts, INTERVAL %(lod)d SECOND) tsi, avg(value) FROM mqtt.mqtt_float"+where+"GROUP BY tsi ORDER BY tsi", args, **kwargs )
        elif lod < 120:
            r = clickhouse.execute( "SELECT ts,favg FROM mqtt.mqtt_minute"+where+"ORDER BY topic,ts", args, **kwargs )
        else:
            r = clickhouse.execute( "SELECT toStartOfInterval(ts, INTERVAL %(lodm)d MINUTE) tsi, avg(favg) FROM mqtt.mqtt_minute"+where+"GROUP BY tsi ORDER BY tsi", args, **kwargs )

        if r:
            x,y = r
            print( "LOD: %s LEN %s %.01f ms %s" % (lod,len(x),(time.time()-t)*1000,self.topic))
            return x, np.array(y)*self.scale
        else:
            return ((),())

class PlotHolder():
    pass

PLOTS = { p[0]:DataStream( *p ) for p in (
    # ( "pv/solis1/pv_power"        , "Solis PV"   , "green" , 1.0 ),
    ( "pv/fronius/grid_port_power", "Fronius PV" , "#00FF80"  , -1.0 ),
    ( "pv/total_pv_power"         , "Total PV"   , "#00FF00" , 1.0 ),
    ( "pv/meter/house_power"      , "House"      , "#8080FF" , 1.0 ),
    ( "pv/solis1/battery_power"   , "Battery"    , "#FFC080", 1.0 ),
    # ( "pv/solis1/bms_battery_power", "Battery BMS" , "yellow",1.0 ),
    ( "pv/meter/total_power"      , "Grid"       , "#FF0000"   , 1.0 ),
    # ( "pv/solis1/fakemeter/active_power", "Fakemeter"       , "blue"   , 1.0 ),
    # ( "pv/solis1/fakemeter/offset", "Offset"       , "magenta"   , 1.0 ),
    # ( "pv/solis1/meter/active_power" , "Solis"   , "cyan"  ,-1.0 ),
)}

DATA = { k:None for k in (
    "pv/solis1/bms_battery_soc",
    "pv/meter/total_export_kwh",
    "pv/meter/total_import_kwh",
    )}

class BokehApp():
    plot_data = []
    last_data_length = None

    def __init__(self):
        io_loop = IOLoop.current()
        self.server = Server(applications = {'/myapp': Application(FunctionHandler(self.make_document))}, io_loop = io_loop, port = 5001)
        self.server.start()
        self.server.show('/myapp')

        for topic in DATA.keys():
            DATA[topic] = get_one( topic )
        print( DATA )

        self.mqtt = MQTTClient("plotter")
        self.mqtt.on_connect    = self.mqtt_on_connect
        self.mqtt.on_message    = self.mqtt_on_message
        self.mqtt.set_auth_credentials( config.MQTT_USER, config.MQTT_PASSWORD )

        io_loop.add_callback(self.astart)
        io_loop.start()

    async def astart( self ):
        await self.mqtt.connect( config.MQTT_BROKER )
        print("MQTT Connected")

    def mqtt_on_connect(self, client, flags, rc, properties):
        print("MQTT On Connect")
        for topic in list(PLOTS.keys())+list(DATA.keys()):
            print( "subscribe", topic )
            self.mqtt.subscribe( topic, qos=0 )

    def mqtt_on_message(self, client, topic, payload, qos, properties):
        p = PLOTS.get(topic)
        y = float(payload)
        if topic in DATA:
            DATA[topic][1] = y
        if p:
            y*=p.scale
            t = np.datetime64(int(time.time()*1000),"ms")
            p.x.append( t )
            p.y.append( y )
            limit = t - np.timedelta64( PLOT_LENGTH, 's' )
            while p.x[0] < limit:
                p.x.popleft()
                p.y.popleft()


    def make_document(self, doc):
        PVDashboard(doc)

class PVDashboard():
    def __init__( self, doc ):
        doc.theme = 'dark_minimal'
        fig = self.fig = figure(   title = 'PV', 
                        sizing_mode = 'stretch_both', 
                        x_axis_type="datetime",
                        tools="undo,redo,reset,save,hover,box_zoom,xwheel_zoom,xpan",
                        active_drag = "xpan",
                        # active_zoom = "xwheel_zoom"
                    )

        self.plots = {}
        for key,stream in PLOTS.items():
            p = PlotHolder()
            p.key    = key
            p.stream = stream
            p.plot   = fig.line( [], [], legend_label=stream.label, color=stream.color, line_width=3 )
            p.last_update = stream.x[-1]
            self.plots[key] = p

        fig.x_range.follow="end"
        fig.x_range.follow_interval = np.timedelta64( PLOT_LENGTH, 's' )
        fig.x_range.range_padding=0

        self.lod_reduce = False
        self.streaming  = True
        self.tick = Metronome( 0.1 )
        self.prev_trange = None

        fig.on_event( bokeh.events.Reset,        self.event_reset )
        fig.on_event( bokeh.events.LODStart,     self.event_lod_start )
        fig.on_event( bokeh.events.LODEnd,       self.event_lod_end )
        fig.on_event( bokeh.events.RangesUpdate, self.event_ranges_update )
        fig.on_event( bokeh.events.MouseWheel,   self.event_generic )
        fig.on_event( bokeh.events.Pan,          self.event_generic )

        doc.add_root(fig)
        doc.add_periodic_callback( self.update, 1000 )

    def get_t_range( self ):
        t1 = self.fig.x_range.start
        t2 = self.fig.x_range.end
        if np.isnan(t1) or np.isnan(t2):
            return None
        return int(t1), int(t2)  # in milliseconds

    def redraw( self, event, force=False ):
        trange = self.get_t_range()
        if not trange:
            return
        trange_change = trange != self.prev_trange
        self.prev_trange = trange
        if not (force or trange_change and self.tick.ticked()):
            return
        tstart = trange[0] * 0.001
        tend   = trange[1] * 0.001
        lod_length=1000 if self.lod_reduce else 5000
        print( tstart, tend, event, self.lod_reduce, lod_length )
        miny=[]
        maxy = []
        for k,ph in self.plots.items():
            ds = ph.plot.data_source
            x,y = ph.stream.get( tstart, tend, lod_length=lod_length )
            if len(y):
                miny.append(y.min())
                maxy.append(y.max())
            ds.data = {"x":x, "y":y }
            ds.trigger('data', ds.data, ds.data )
        if miny:
            self.fig.y_range.start = max(-4000,min(miny))
            self.fig.y_range.end   = min(12000,max(maxy))
        self.tick.ticked()

    def event_lod_start( self, event ):
        print( event )
        self.streaming  = False
        self.lod_reduce = True
        self.redraw( event )

    def event_lod_end( self, event ):
        print( event )
        self.lod_reduce = False
        self.redraw( event, True )

    def event_reset( self, event ):
        self.streaming = True
        self.update()

    def event_ranges_update( self, event ):
        self.redraw( event )

    def event_generic( self, event ):
        self.redraw( event )

    def update( self ):
        if not self.streaming:
            return
        self.fig.title.text = "PV: Batt %d%% Import %.03f Export %.03f Total %.03f kWh" % (
                DATA["pv/solis1/bms_battery_soc"][1],
                DATA["pv/meter/total_import_kwh"][1]-DATA["pv/meter/total_import_kwh"][0],
                DATA["pv/meter/total_export_kwh"][1]-DATA["pv/meter/total_export_kwh"][0],
                DATA["pv/meter/total_import_kwh"][1]-DATA["pv/meter/total_import_kwh"][0]-(DATA["pv/meter/total_export_kwh"][1]-DATA["pv/meter/total_export_kwh"][0])
            )
        for k,ph in self.plots.items():
            ds = ph.plot.data_source
            s  = ph.stream
            if ph.last_update == s.x[-1]:
                continue
            ph.last_update = s.x[-1]
            ds.data = {"x":np.array( s.x ), "y":np.array( s.y )}
            # print( s.x[-1] )
            ds.trigger('data', ds.data, ds.data )
            self.prev_trange = self.get_t_range()


if __name__ == '__main__':
    app = BokehApp()


