

import random, time, clickhouse_driver, collections
from tornado.ioloop import IOLoop
from bokeh.server.server import Server
from bokeh.application import Application
from bokeh.application.handlers.function import FunctionHandler
from bokeh.plotting import figure, ColumnDataSource
from threading import Thread

import asyncio, time, traceback, logging, sys, datetime
import numpy as np
# from numpy_ringbuffer import RingBuffer

from gmqtt import Client as MQTTClient
import config, grugbus

from bokeh.plotting import figure, curdoc
from bokeh.driving import linear
import random

PLOT_LENGTH = 200    # seconds

clickhouse = clickhouse_driver.Client('localhost', user=config.CLICKHOUSE_USER, password=config.CLICKHOUSE_PASSWORD )

class PlotSource( object ):
    def __init__( self, topic, label, color, scale ):
        self.topic = topic
        self.label = label
        self.color = color
        self.scale = scale
        tstart = datetime.datetime.now() - datetime.timedelta( seconds=PLOT_LENGTH )
        x,y = clickhouse.execute( "SELECT ts,value FROM mqtt.mqtt_float WHERE topic=%(topic)s AND ts > %(start)s ORDER BY topic,ts",
                {"topic":topic, "start":tstart}, columnar=True, settings={"use_numpy":True} )
        # self.x = RingBuffer( capacity=max(len(x), 10), dtype=np.datetime64(datetime.datetime.now()).dtype )
        # self.y = RingBuffer( capacity=max(len(x), 10), dtype=np.float64 )
        self.x = collections.deque( x + np.timedelta64( 1, 'h' ) )
        self.y = collections.deque( np.array(y)*scale )

class PlotHolder():
    pass

PLOTS = { p[0]:PlotSource( *p ) for p in (
    ( "pv/solis1/pv_power"        , "Solis PV"   , "green" , 1.0 ),
    ( "pv/total_pv_power"         , "Total PV"   , "green" , 1.0 ),
    ( "pv/meter/house_power"      , "House"      , "white" , 1.0 ),
    ( "pv/solis1/battery_power"   , "Battery"    , "orange",1.0 ),
    ( "pv/solis1/bms_battery_power", "Battery BMS" , "yellow",1.0 ),
    ( "pv/meter/total_power"      , "Grid"       , "red"   , 1.0 ),
    ( "pv/solis1/fakemeter/active_power", "Fakemeter"       , "blue"   , 1.0 ),
    ( "pv/solis1/fakemeter/offset", "Offset"       , "magenta"   , 1.0 ),
    ( "pv/solis1/meter/active_power" , "Solis"   , "cyan"  ,-1.0 ),
    # ( "pv/fronius/grid_port_power", "Fronius PV" , "blue"  , -1.0 ),
)}

class BokehApp():
    plot_data = []
    last_data_length = None

    def __init__(self):
        io_loop = IOLoop.current()
        self.server = Server(applications = {'/myapp': Application(FunctionHandler(self.make_document))}, io_loop = io_loop, port = 5001)
        self.server.start()
        self.server.show('/myapp')

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
        for topic in PLOTS.keys():
            print( "subscribe", topic )
            self.mqtt.subscribe( topic, qos=0 )

    def mqtt_on_message(self, client, topic, payload, qos, properties):
        p = PLOTS.get(topic)
        if p:
            y = float(payload)*p.scale
            t = np.datetime64(datetime.datetime.now())
            p.x.append( t )
            p.y.append( y )
            limit = t - np.timedelta64( PLOT_LENGTH, 's' )
            while p.x[0] < limit:
                p.x.popleft()
                p.y.popleft()

    def make_document(self, doc):
        doc.theme = 'dark_minimal'
        fig = figure(   title = 'PV', 
                        sizing_mode = 'stretch_both', 
                        x_axis_type="datetime",
                        tools="undo,redo,reset,save,hover,box_zoom,xwheel_zoom,xpan",
                        active_drag = "xpan",
                        # active_zoom = "xwheel_zoom"
                    )

        print( fig.toolbar.active_inspect )
        plots = {}
        for k,psource in PLOTS.items():
            p = PlotHolder()
            p.key    = k
            p.source = psource
            p.plot   = fig.line( [], [], legend_label=psource.label, color=psource.color, line_width=2 )
            p.last_update = psource.x[0]
            plots[k] = p

        fig.x_range.follow="end"
        fig.x_range.follow_interval = np.timedelta64( PLOT_LENGTH, 's' )
        fig.x_range.range_padding=0

        def update():
            for k,ph in plots.items():
                ds = ph.plot.data_source
                s  = ph.source
                ds.data = {"x":np.array( s.x ), "y":np.array( s.y )}
                ds.trigger('data', ds.data, ds.data )

            # if self.last_data_length is not None and self.last_data_length != len(self.plot_data):
                # source.stream(self.plot_data[-1])
            # self.last_data_length = len(self.plot_data)

        doc.add_root(fig)
        doc.add_periodic_callback( update, 250 )

if __name__ == '__main__':
    app = BokehApp()


