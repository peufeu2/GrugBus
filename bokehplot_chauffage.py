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

PLOT_LENGTH = 3600    # seconds
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
    def __init__( self, pane, topic, label, color, scale, extra_plot_args={}, visible=True, mode="" ):
        self.pane = pane
        self.topic = topic
        self.label = label
        self.color = color
        self.scale = scale
        self.extra_args = extra_plot_args
        self.mode = mode
        self.visible = visible
        self.load()

    def load( self ):
        x,y = self.get( time.time()-PLOT_LENGTH )
        self.x = collections.deque( x )
        self.y = collections.deque( y )
        print( "Loaded %d for %s" % (len(self.x), self.topic))

    def get( self, tstart, tend=None, lod_length=3600 ):
        # All times in database are in UTC. Clickhouse treats integer timestamps as UTC too,
        # and will accept DateTime between (UTC int timestamp) and (UTC int timestamp)
        if isinstance( tstart, datetime.datetime ):
            tstart = tstart.timestamp()
            if tend:
                tehd = tend.timestamp()
        span = (tend or time.time())-tstart
        assert span>=0

        # dynamic level of detail
        lod = int(round( span/lod_length ) or 1)
        tstart -= TIME_SHIFT_S
        if tend:
            tend -= TIME_SHIFT_S

        args   = { "topic":self.topic, "tstart":tstart, "tend":tend, "lod":lod, "lodm":lod//60 }
        kwargs = { "columnar":True, "settings":{"use_numpy":True} }            
        if tend:    ts_cond = "ts BETWEEN %(tstart)s AND %(tend)s"
        else:       ts_cond = "ts > %(tstart)s"
        where = " WHERE topic=%(topic)s AND " + ts_cond + " "

        t=time.time()
        if lod >= 6000:
            r = clickhouse.execute( "SELECT toStartOfInterval(ts, INTERVAL %(lod)d SECOND) tsi, avg(favg) FROM mqtt.mqtt_100minute"+where+"GROUP BY tsi ORDER BY tsi", args, **kwargs )
        elif lod >= 600:
            r = clickhouse.execute( "SELECT toStartOfInterval(ts, INTERVAL %(lod)d SECOND) tsi, avg(favg) FROM mqtt.mqtt_10minute"+where+"GROUP BY tsi ORDER BY tsi", args, **kwargs )
        elif lod >= 60:
            r = clickhouse.execute( "SELECT toStartOfInterval(ts, INTERVAL %(lod)d SECOND) tsi, avg(favg) FROM mqtt.mqtt_minute"+where+"GROUP BY tsi ORDER BY tsi", args, **kwargs )
        elif lod > 1:
            r = clickhouse.execute( "SELECT toStartOfInterval(ts, INTERVAL %(lod)d SECOND) tsi, avg(value) FROM mqtt.mqtt_float"+where+"GROUP BY tsi ORDER BY tsi", args, **kwargs )
        else:
            r = clickhouse.execute( "SELECT ts,value FROM mqtt.mqtt_float"+where+"ORDER BY topic,ts", args, **kwargs )

        if not r:  # no data, get latest available row
            r = clickhouse.execute( "SELECT ts, value FROM mqtt.mqtt_float WHERE topic=%(topic)s ORDER BY ts DESC LIMIT 1", args, **kwargs )                
            
        if r:
            x,y = r
            print( "LOD: %s LEN %s %.01f ms %s" % (lod,len(x),(time.time()-t)*1000,self.topic))
            y = np.array(y)*self.scale
            if "day" in self.mode:
                y -= y[0]
            x += TIME_SHIFT
            return x, y
        else:
            return ((),())

class PlotHolder():
    pass

PLOTS = { p[1]:DataStream( *p ) for p in (
    ( 0, "chauffage/depart"             , "depart"              , "#FF0000"  , 1.0             , {} , False),
    ( 0, "chauffage/retour"             , "retour"              , "#FF0000"  , 1.0             , {} , False),
    ( 0, "chauffage/pac_depart"         , "pac_depart"          , "#FF8000"  , 1.0             , {} , False),
    ( 0, "chauffage/pac_retour"         , "pac_retour"          , "#FF8000"  , 1.0             , {} , False),
    ( 0, "chauffage/debit"              , "debit"               , "#FFFFFF"  , 1.0             , {} , False),
    ( 0, "chauffage/et_bureau"          , "et_bureau"           , "#808080"  , 1.0             , {} , False),
    ( 0, "chauffage/et_pcbt_depart"     , "et_pcbt_depart"      , "#8000FF"  , 1.0             , {} , False),
    ( 0, "chauffage/et_pcbt_retour"     , "et_pcbt_retour"      , "#8000FF"  , 1.0             , {} , False),
    ( 0, "chauffage/et_pcbt_ambient"    , "et_pcbt_ambient"     , "#8000FF"  , 1.0             , {} , True ),
    ( 0, "chauffage/ext_parking"        , "ext_parking"         , "#808000"  , 1.0             , {} , True ),
    ( 0, "chauffage/ext_sous_balcon"    , "ext_sous_balcon"     , "#808000"  , 1.0             , {} , True ),
    ( 0, "chauffage/rc_pc_cuisine"      , "rc_pc_cuisine"       , "#0000FF"  , 1.0             , {} , True ),
    ( 0, "chauffage/rc_pc_pcbt_ambient" , "rc_pc_pcbt_ambient"  , "#00FF00"  , 1.0             , {} , True ),
    ( 0, "chauffage/rc_pc_pcbt_depart"  , "rc_pc_pcbt_depart"   , "#00FF00"  , 1.0             , {} , False),
    ( 0, "chauffage/rc_pc_pcbt_retour"  , "rc_pc_pcbt_retour"   , "#00FF00"  , 1.0             , {} , False),
    ( 0, "chauffage/rc_pf_pcbt_ambient" , "rc_pf_pcbt_ambient"  , "#0080FF"  , 1.0             , {} , True ),
    ( 0, "chauffage/rc_pf_pcbt_depart"  , "rc_pf_pcbt_depart"   , "#0080FF"  , 1.0             , {} , False),
    ( 0, "chauffage/rc_pf_pcbt_retour"  , "rc_pf_pcbt_retour"   , "#0080FF"  , 1.0             , {} , False),
    ( 0, "chauffage/rc_pf_che"          , "rc_pf_che"           , "#FF8000"  , 1.0             , {} , False),
    ( 0, "chauffage/debit"              , "debit"               , "#808080"  , 1.0             , {} , False),
    ( 0, "chauffage/pompe"              , "pompe"               , "#80FF80"  , 1.0             , {} , False),
    ( 0, "chauffage/pac_puits"          , "pac_puits"               , "#00FFFF"  , 1.0             , {} , False),
    ( 0, "chauffage/pac_rejet"          , "pac_rejet"               , "#008080"  , 1.0             , {} , False),
)}

class BokehApp():
    plot_data = []
    last_data_length = None

    def __init__(self):
        io_loop = IOLoop.current()
        self.server = Server(applications = {'/myapp': Application(FunctionHandler(self.make_document))}, io_loop = io_loop, port = 5002)
        self.server.start()
        # self.server.show('/myapp')

        self.mqtt = MQTTClient("plotter_chauffage")
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
        y = float(payload)
        if p:
            y*=p.scale
            t = np.datetime64(int(time.time()*1000),"ms") + TIME_SHIFT
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

        self.figs = { stream.pane:None for key,stream in PLOTS.items() }
        self.figs[0] = figure(   title = 'PV', 
                        sizing_mode = 'stretch_both', 
                        x_axis_type="datetime",
                        tools="undo,redo,reset,save,hover,box_zoom,xwheel_zoom,xpan",
                        active_drag = "xpan",
                        # active_zoom = "xwheel_zoom"
                    )

        if 1 in self.figs:
            self.figs[1] = figure(   title = 'Inverter', 
                            sizing_mode = 'stretch_both', 
                            x_axis_type="datetime",
                            tools="hover",
                            # active_drag = "xpan",
                        )

            # self.figs[1].extra_y_ranges = {"temp": bokeh.models.Range1d(start=50, end=70)}
            # self.figs[1].add_layout(bokeh.models.LinearAxis(y_range_name="temp"), 'right')

        self.range_slider = Slider(start=PLOT_LENGTH_MIN, end=PLOT_LENGTH_MAX, value=PLOT_LENGTH, step=100, title="Range")
        self.range_slider.on_change("value", self.range_slider_on_change)

        self.lod_slider_value = 1
        self.lod_slider = Slider(start=1, end=100, value=self.lod_slider_value, step=1, title="Smooth")
        self.lod_slider.on_change("value", self.lod_slider_on_change)
        self.lod_slider_value = 1

        self.plots = {}
        for key,stream in PLOTS.items():
            p = PlotHolder()
            p.key    = key
            p.stream = stream
            p.plot   = self.figs[stream.pane].line( [], [], legend_label=stream.label, color=stream.color, line_width=3, **stream.extra_args )
            p.plot.visible = stream.visible
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
        for key,stream in PLOTS.items():
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
        lod_length=1000 if self.lod_reduce else int(5000 * 10/(10+self.lod_slider_value))
        print( tstart, tend, event, self.lod_reduce, lod_length )
        miny = []
        maxy = []
        for k,ph in self.plots.items():
            if ph.plot.visible:
                ds = ph.plot.data_source
                x,y = ph.stream.get( tstart, tend, lod_length=lod_length )
                if len(y):
                    print(k, y.min(), y.max())
                    miny.append(y.min())
                    maxy.append(y.max())
                ds.data = {"x":x, "y":y }
                ds.trigger('data', ds.data, ds.data )
        if miny:
            # for fig in self.figs.values():
            fig = self.figs[0]
            fig.y_range.start = max(-10,min(miny))
            fig.y_range.end   = min(50,max(maxy))
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

    def update_title( self ):
        pass

    def update( self ):
        if not self.streaming:
            return
        for k,ph in self.plots.items():
            ds = ph.plot.data_source
            s  = ph.stream
            if not s.x: # is there data?
                continue
            if ph.last_update == s.x[-1]:
                continue
            ph.last_update = s.x[-1]
            ds.data = {"x":np.array( s.x ), "y":np.array( s.y )}
            # print( s.x[-1] )
            ds.trigger('data', ds.data, ds.data )
            self.prev_trange = self.get_t_range()


if __name__ == '__main__':
    app = BokehApp()


