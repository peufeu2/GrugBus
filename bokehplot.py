import random, time, clickhouse_driver, collections
import asyncio, time, traceback, logging, sys, datetime
import numpy as np

from tornado.ioloop import IOLoop
import bokeh.events, bokeh.layouts
from bokeh.server.server import Server
from bokeh.application import Application
from bokeh.application.handlers.function import FunctionHandler
from bokeh.plotting import figure, ColumnDataSource, figure, curdoc
from bokeh.io import show
from bokeh.models import CustomJS, Slider

from gmqtt import Client as MQTTClient

import config
from misc import *

PLOT_LENGTH = 200    # seconds
PLOT_LENGTH_MIN = 200    # seconds
PLOT_LENGTH_MAX = 3600    # seconds
TIME_SHIFT_S = 3600*2
TIME_SHIFT = np.timedelta64( TIME_SHIFT_S, 's' )

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
    def __init__( self, pane, topic, label, color, scale, extra_plot_args={}, mode="", minmaxavg="avg" ):
        self.pane = pane
        self.topic = topic
        self.label = label
        self.color = color
        self.scale = scale
        self.extra_args = extra_plot_args
        self.mode = mode
        if minmaxavg in ("avg","min","max"):
            self.sel1 = "%s(f%s)" % (minmaxavg,minmaxavg)
            self.sel2 = "%s(value)"%minmaxavg
        self.load()

    def load( self ):
        x,y = self.get( time.time()-PLOT_LENGTH )
        self.x = collections.deque( x )
        self.y = collections.deque( y )
        if self.x:
            print( "Loaded %d for %s : %s-%s" % (len(self.x), self.topic, self.x[0], self.x[-1]))

    def add( self, y ):
        y*=self.scale
        t = np.datetime64(datetime.datetime.now(), "ms")
        if self.x:
            self.x.append( t )
            self.y.append( self.y[-1] )
        self.x.append( t )
        self.y.append( y )
        self.purge_old()

    def purge_old( self ):
        t = np.datetime64(datetime.datetime.now(), "ms")
        limit = t - np.timedelta64( PLOT_LENGTH, 's' )
        while self.x and self.x[0] < limit:
            x = self.x.popleft()
            self.y.popleft()

    def get( self, tstart, tend=None, lod_length=3600 ):
        # All times in database are in UTC. Clickhouse treats integer timestamps as UTC too,
        # and will accept DateTime between (UTC int timestamp) and (UTC int timestamp)
        if isinstance( tstart, datetime.datetime ):
            tstart = tstart.timestamp()
            if tend:
                tend = tend.timestamp()
        span = (tend or time.time())-tstart
        assert span>=0

        # dynamic level of detail
        lod = int(round( span/lod_length ) or 1)
        overscan = 60
        tstart -= TIME_SHIFT_S + overscan
        if tend:
            tend -= TIME_SHIFT_S - overscan


        args   = { "topic":self.topic, "tstart":tstart, "tend":tend, "lod":lod, "lodm":lod//60 }
        kwargs = { "columnar":True, "settings":{"use_numpy":True} }            
        if tend:    ts_cond = "ts BETWEEN toDateTime64(%(tstart)s,1) AND toDateTime64(%(tend)s,1)"
        else:       ts_cond = "ts > toDateTime64(%(tstart)s,1)"
        where = " WHERE topic=%(topic)s AND " + ts_cond + " "

        # if self.topic=="pv/meter/total_power":
        #     r = clickhouse.execute( "SELECT avg(value) FROM mqtt.mqtt_float"+where+" AND value>0", args, **kwargs )
        #     print( r[0] )

        t=time.time()
        if lod >= 6000:
            r = clickhouse.execute( "SELECT toStartOfInterval(ts, INTERVAL %(lod)d SECOND) tsi, "+self.sel1+" FROM mqtt.mqtt_100minute"+where+"GROUP BY tsi ORDER BY tsi", args, **kwargs )
        elif lod >= 600:
            r = clickhouse.execute( "SELECT toStartOfInterval(ts, INTERVAL %(lod)d SECOND) tsi, "+self.sel1+" FROM mqtt.mqtt_10minute"+where+"GROUP BY tsi ORDER BY tsi", args, **kwargs )
        elif lod >= 60:
            r = clickhouse.execute( "SELECT toStartOfInterval(ts, INTERVAL %(lod)d SECOND) tsi, "+self.sel1+" FROM mqtt.mqtt_minute"+where+"GROUP BY tsi ORDER BY tsi", args, **kwargs )
        elif lod > 1:
            r = clickhouse.execute( "SELECT toStartOfInterval(ts, INTERVAL %(lod)d SECOND) tsi, "+self.sel2+" FROM mqtt.mqtt_float"+where+"GROUP BY tsi ORDER BY tsi", args, **kwargs )
        else:
            r = clickhouse.execute( "SELECT ts,value FROM mqtt.mqtt_float"+where+"ORDER BY topic,ts", args, **kwargs )

        if not r:  # no data, get latest available row
            r = clickhouse.execute( "SELECT ts, value FROM mqtt.mqtt_float WHERE topic=%(topic)s ORDER BY ts DESC LIMIT 1", args, **kwargs )                
            
        if r:
            x,y = r
            # print( "%6d %s" % (len(x),self.topic) )
            if len(x) < 500:    # improve visibility of steps
                dt = np.timedelta64( 1, 'ms' )
                x=np.repeat(x,2)
                y=np.repeat(y,2)
                x[1:-1:2] = x[2::2] - dt

            # print( "LOD: %s LEN %s %.01f ms %s" % (lod,len(x),(time.time()-t)*1000,self.topic))
            y = np.array(y)*self.scale
            if "day" in self.mode:
                y -= y[0]
            x += TIME_SHIFT
            return x, y
        else:
            return ((),())

class PlotHolder():
    pass

DATASTREAMS = { p[1]:DataStream( *p ) for p in (
    # ( 0, "pv/fronius/grid_port_power"   , "Fronius PV" , "#008000"  , -4.5  , {} ),
    ( 0, "pv/total_pv_power"                , "Total PV"   , "#00FF00"  , 1.0   , {} ),
    ( 0, "pv/solis1/pv_power"               , "PV Solis 1" , "#00C000"  , 1.0   , {} ),
    ( 0, "pv/solis2/pv_power"               , "PV Solis 2" , "#008000"  , 1.0   , {} ),

    ( 0, "pv/meter/house_power"             , "House"      , "#8080FF"  , 1.0   , {} ),
    ( 0, "pv/meter/total_power"             , "Grid"       , "#FF0000"  , 1.0   , {} ),

    ( 0, "pv/solis1/battery_power"          , "Battery"         , "#C08040"  , 1.0   , {} ),
    
    ( 0, "pv/solis1/input_power"            , "Battery (proxy)" , "#FFC080"  , 1.0   , {} ),
    # ( 0, "pv/solis1/bms_battery_power"          , "Battery"    , "#FFC080"  , 1.0   , {} ),
    ( 0, "pv/solis1/meter/active_power"     , "Inverter"      , "cyan"     , 1.0   , {} ),

    # ( 0, "pv/solis1/fakemeter/active_power" , "FakeMeter"      , "#FFFFFF"     , 1.0   , {"visible":False} ),
    ( 0, "pv/evse/rwr_current_limit"        , "EVSE ILim"   , "#FFFFFF"   , 235 , {"visible":False} ),
    # ( 0, "pv/evse/meter/current"            , "EVSE I (real)"    , "#808080"   , 235, {"visible":False} ),
    ( 0, "pv/evse/meter/active_power"       , "EVSE"        , "#FF80FF"  , 1.0   , {} ),
    ( 0, "pv/router/excess_avg"             , "Route excess", "#FF00FF"  , -1.0   , {} ),
    # ( 0, "pv/router/excess_avg_nobat"   , "Route excess nobat", "#8000FF"  , -1.0   , {} ),
    # ( 0, "pv/solis1/meter_total_active_power"         , "SMAP"   , "#000080"   , -1.0, {"visible":False} ),

    # ( 0, "pv/solis1/dc_bus_voltage"   , "dc_bus_voltage", "#8000FF"  , -10.0   , {} ),
    # ( 0, "cmd/pv/write/rwr_battery_discharge_power_limit"   , "rwr_battery_discharge_power_limit", "#8000FF"  , 1.0   , {} ),

    ( 0, "pv/solis1/mppt1_power"       , "mppt1_power" , "#008000"  , 1.0, {"visible":False} ),
    ( 0, "pv/solis1/mppt2_power"       , "mppt2_power" , "#00C000"  , 1.0, {"visible":False} ),

    # ( 1, "pv/meter/phase_1_line_to_neutral_volts"         , "PH1V"   , "orange"   , 1.0, {} ),
    # ( 1, "pv/meter/phase_2_line_to_neutral_volts"         , "PH2V"   , "orange"   , 1.0, {} ),
    # ( 1, "pv/meter/phase_3_line_to_neutral_volts"         , "PH3V"   , "orange"   , 1.0, {} ),


    # ( 1, "pv/solis1/bms_battery_current" , "Battery current" , "#FFC080"  ,1.0  , {"visible":False} ),
    ( 1, "pv/solis1/bms_battery_soc"     , "Battery SOC"   , "green"    , 1.0   , {"visible":False} ),
    ( 1, "pv/solis1/temperature"         , "Temperature"   , "orange"   , 1.0   , {"visible":False} ),
    ( 1, "pv/evse/virtual_current_limit" , "EVSE ILim (virtual)" , "#FF00FF"   , 1.0, {"visible":False} ),
    # ( 1, "pv/evse/current"               , "EVSE I (real)"    , "#FFFFFF"   , 1.0, {"visible":False} ),
    ( 1, "pv/evse/energy"               , "EVSE kWh"    , "#FFFFFF"   , 1.0, {"visible":False} ),
    # ( 1, "pv/solis1/battery_current"     , "Bat current"    , "#FF8000"   , 1.0, {"visible":False} ),
    # ( 1, "pv/solis1/battery_voltage"     , "Bat voltage"    , "#FFFF00"   , 1.0, {"visible":False} ),
    ( 1, "pv/solis1/battery_max_charge_current"     , "Bat max current"    , "#0080FF"   , 1.0, {"visible":False} ),

    ( 1, "pv/meter/req_time"               , "meter req_time"    , "#FFFFFF"   , 1.0, {}, "", "max" ),
    ( 1, "pv/evse/req_time"                , "EVSE req_time"     , "#FFFF00"   , 1.0, {}, "", "max" ),
    ( 1, "pv/meter/req_period"             , "req_period"        , "#00FF00"   , 1.0, {}, "", "max" ),
    # ( 1, "pv/solis1/fakemeter/lag"               , "lag"    , "#FF00FF"   , 1.0, {} ),


    # ( 1, "pv/solis1/dc_bus_voltage"      , "DC Bus"        ,    "#FFFFFF"     , 1.0, {} ),
    # ( 1, "pv/solis1/mppt1_voltage"       , "mppt1_voltage" , "#FFFF00"  , 1.0, {} ),
    # ( 1, "pv/solis1/mppt2_voltage"       , "mppt2_voltage" , "#FFFFFF"  , 1.0, {} ),
    # ( 1, "pv/solis1/mppt1_current"       , "mppt1_current" , "#FFFF00"  , 1.0, {} ),
    # ( 1, "pv/solis1/mppt2_current"       , "mppt2_current" , "#FFFFFF"  , 1.0, {} ),
    # ( 1, "pv/solis1/bms_battery_charge_current_limit"      , "BMS max charge Amps"    , "blue"  ,1.0      , {} ),
    # ( 1, "pv/solis1/bms_battery_discharge_current_limit"   , "BMS max discharge Amps" , "red"  ,1.0      , {} ),

    # ( 1, "pv/solis1/energy_generated_today"     , "Energy generated"   , "green"  ,1.0      , {} , "day" ),
    # ( 1, "pv/solis1/meter/import_active_energy"     , "Solis import"   , "#FFC000"  ,1.0    , {} , "day" ),
    # ( 1, "pv/solis1/meter/export_active_energy"     , "Solis export"   , "#00FF80"  ,1.0    , {} , "day" ),
    # ( 1, "pv/meter/total_import_kwh"     , "Grid import"   , "red"  ,1.0                    , {} , "day" ),
    # ( 1, "pv/meter/total_export_kwh"     , "Grid export"   , "cyan"  ,1.0                   , {} , "day" ),
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
        # self.server.show('/myapp')

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
        # for topic in list(DATASTREAMS.keys())+list(DATA.keys()):
        for topic in DATASTREAMS.keys():
            print( "subscribe", topic )
            self.mqtt.subscribe( topic, qos=0 )

    def mqtt_on_message(self, client, topic, payload, qos, properties):
        p = DATASTREAMS.get(topic)
        y = float(payload)
        if p:
            p.add( y )

    def make_document(self, doc):
        PVDashboard(doc)

class PVDashboard():
    def __init__( self, doc ):
        doc.theme = 'dark_minimal'

        self.figs = { stream.pane:None for key,stream in DATASTREAMS.items() }
        self.figs[0] = figure(   title = 'PV', 
                        sizing_mode = 'stretch_both', 
                        x_axis_type="datetime",
                        tools="undo,redo,reset,save,hover,box_zoom,xwheel_zoom,wheel_zoom,xpan",
                        active_drag = "xpan",
                        # active_zoom = "xwheel_zoom"
                    )

        if 1 in self.figs:
            self.figs[1] = figure(   title = 'Inverter', 
                            sizing_mode = 'stretch_both', 
                            x_axis_type="datetime",
                            tools="undo,redo,reset,save,hover,box_zoom,xwheel_zoom,wheel_zoom,xpan",
                            # active_drag = "xpan",
                        )

            # self.figs[1].extra_y_ranges = {"temp": bokeh.models.Range1d(start=50, end=70)}
            # self.figs[1].add_layout(bokeh.models.LinearAxis(y_range_name="temp"), 'right')

        for fig in self.figs.values():
            fig.xaxis.ticker.desired_num_ticks = 20

        self.range_slider = Slider(start=PLOT_LENGTH_MIN, end=PLOT_LENGTH_MAX, value=PLOT_LENGTH, step=100, title="Range")
        self.range_slider.on_change("value", self.range_slider_on_change)

        self.lod_slider_value = 1
        self.lod_slider = Slider(start=1, end=100, value=self.lod_slider_value, step=1, title="Smooth")
        self.lod_slider.on_change("value", self.lod_slider_on_change)
        self.lod_slider_value = 1

        self.plots = {}
        for key,stream in DATASTREAMS.items():
            p = PlotHolder()
            p.key    = key
            p.stream = stream
            p.plot   = self.figs[stream.pane].line( [], [], legend_label=stream.label, color=stream.color, line_width=3, **stream.extra_args )
            p.last_update = 0
            if stream.x:    # it can be empty if there is no data for the requested range
                p.last_update = stream.x[-1]

            self.plots[key] = p

        for fig in self.figs.values():
            # fig.x_range.follow="end"
            # fig.x_range.follow_interval = np.timedelta64( PLOT_LENGTH, 's' )
            # fig.x_range.range_padding=0
            fig.legend.location = "top_left"
            fig.legend.click_policy="hide"

            fig.on_event( bokeh.events.Reset,        self.event_reset )
            fig.on_event( bokeh.events.LODStart,     self.event_lod_start )
            fig.on_event( bokeh.events.LODEnd,       self.event_lod_end )
            fig.on_event( bokeh.events.RangesUpdate, self.event_ranges_update )
            fig.on_event( bokeh.events.MouseWheel,   self.event_generic )
            fig.on_event( bokeh.events.Pan,          self.event_generic )

            # fig.lod_factor    = 10
            # fig.lod_interval  = 100
            # fig.lod_threshold = 100
            # fig.lod_timeout   = 1000

            fig.lod_factor    = 10
            fig.lod_interval  = 100
            fig.lod_threshold = None
            fig.lod_timeout   = 1000

        self.lod_reduce = False
        self.streaming  = True
        self.tick = Metronome( 0.1 )
        self.prev_trange = None
        self.update_title()

        # doc.add_root(fig)

        doc.add_root(
            bokeh.layouts.column( 
                *( list( self.figs.values() ) + [
                # bokeh.layouts.row( self.range_slider )
                bokeh.layouts.row( self.range_slider, self.lod_slider )]),
                sizing_mode="stretch_both"
                )
        )

        doc.add_periodic_callback( self.update, 500 )
        doc.add_periodic_callback( self.update_title, 10000 )

    def lod_slider_on_change( self, attr, oldvalue, value ):
        self.lod_slider_value = value
        if not self.streaming and self.tick.ticked():
            self.redraw( attr, force=True )

    def range_slider_on_change( self, attr, oldvalue, value ):
        value = min(PLOT_LENGTH_MAX,max(PLOT_LENGTH_MIN,value))
        global PLOT_LENGTH
        PLOT_LENGTH = value
        for key,stream in DATASTREAMS.items():
            stream.load()
        for fig in self.figs.values():
            fig.x_range.follow_interval = np.timedelta64( PLOT_LENGTH, 's' )
        # self.redraw( attr )

    def get_t_range( self ):
        fig = self.figs[0]
        t1 = fig.x_range.start
        t2 = fig.x_range.end
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

        # copy to other plot panes
        for k, fig in self.figs.items():
            if k!=0:
                fig.x_range.start, fig.x_range.end = trange

        tstart = trange[0] * 0.001
        tend   = trange[1] * 0.001
        lod_length=300 if self.lod_reduce else int(5000 * 10/(10+self.lod_slider_value))
        # print( tstart, tend, event, self.lod_reduce, lod_length )

        for fig_k, fig in self.figs.items():
            for k,ph in self.plots.items():
                if ph.plot.visible and ph.stream.pane == fig_k:
                    ds = ph.plot.data_source
                    x,y = ph.stream.get( tstart, tend, lod_length=lod_length )
                    ds.data = {"x":x, "y":y }
                    ds.trigger('data', ds.data, ds.data )
        self.autorange()
        self.tick.ticked()

    def autorange( self ):
        for fig_k, fig in self.figs.items():
            miny = []
            maxy = []
            for k,ph in self.plots.items():
                if ph.plot.visible and ph.stream.pane == fig_k:
                    # print("autorange",ph.key)
                    ds = ph.plot.data_source
                    x = ds.data["x"]
                    y = ds.data["y"]
                    if len(y):
                        miny.append(y.min())
                        maxy.append(y.max())
            if miny:
                # for fig in self.figs.values():
                fig.y_range.start = min(miny)
                fig.y_range.end   = max(maxy)
                # print( min(miny), max(maxy))

    def event_lod_start( self, event ):
        # print( event )
        self.streaming  = False
        # self.lod_reduce = True
        self.redraw( event )

    def event_lod_end( self, event ):
        # print( event )
        self.lod_reduce = False
        self.redraw( event, True )

    def event_reset( self, event ):
        self.streaming = True
        for fig in self.figs.values():
            fig.x_range.follow="end"
            fig.x_range.follow_interval = np.timedelta64( PLOT_LENGTH, 's' )
            fig.x_range.range_padding=0
        self.update()

    def event_ranges_update( self, event ):
        self.redraw( event )

    def event_generic( self, event ):
        self.redraw( event )

    def update_title( self ):
        for topic in DATA.keys():
            DATA[topic] = get_one( topic )
        self.figs[0].title.text = "PV: Batt %d%% Import %.03f Export %.03f Total %.03f kWh" % (
                DATA["pv/solis1/bms_battery_soc"][1],
                DATA["pv/meter/total_import_kwh"][1]-DATA["pv/meter/total_import_kwh"][0],
                DATA["pv/meter/total_export_kwh"][1]-DATA["pv/meter/total_export_kwh"][0],
                DATA["pv/meter/total_import_kwh"][1]-DATA["pv/meter/total_import_kwh"][0]-(DATA["pv/meter/total_export_kwh"][1]-DATA["pv/meter/total_export_kwh"][0])
            )

    def update( self ):
        if not self.streaming:
            return
        for k,ph in self.plots.items():
            ds = ph.plot.data_source
            s  = ph.stream

            s.purge_old()

            if not s.x:                     # is there data?
                continue
            if ph.last_update == s.x[-1]:   # is there new data?
                continue                    # no new data, so no need to redraw
            ph.last_update = s.x[-1]

            # send data to browser
            ds.data = {"x":np.array( s.x ), "y":np.array( s.y )}
            ds.trigger('data', ds.data, ds.data )
            
            trange = self.prev_trange = self.get_t_range()
            if trange:
                for k, fig in self.figs.items():
                    if k!=0:
                        fig.x_range.start, fig.x_range.end = trange
        # self.autorange()


if __name__ == '__main__':
    app = BokehApp()


