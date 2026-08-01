#!/usr/bin/python
# -*- coding: utf-8 -*-

import os, time, sys, traceback, asyncio, uvloop, logging, orjson, re, gmqtt
from path import Path

import config

# logging.basicConfig( #encoding='utf-8', 
#                      level=logging.INFO,
#                      format='[%(asctime)s] %(levelname)s:%(message)s',
#                      handlers=[logging.FileHandler(filename=Path(__file__).stem+'.log'), 
#                             logging.StreamHandler(stream=sys.stdout)])
# log = logging.getLogger(__name__)

Z2M_TOPIC = "z2m"

if 1:
    #   Sonoff SNZB-02P: confirmed works without pushing button
    #

    #   TH#             Temp min_report max_report 
    #   None=default

    REP_SETTINGS = { }

    def init1():
        TEMP =  {
                    "cluster"                 : "msTemperatureMeasurement",
                    "attribute"               : "measuredValue",
                }
        HUMI = {
                    "cluster"                 : "msRelativeHumidity",
                    "attribute"               : "measuredValue",
                }

        MINR = "minimum_report_interval"
        MAXR = "maximum_report_interval"
        REP  = "reportable_change"

        # Conserve battery for freezer
        for th_id in 47,48:
            REP_SETTINGS[ "th%d"%th_id ]  = [ TEMP|{MINR: 600, MAXR: 3600, REP: 50 }, HUMI|{MINR: 600, MAXR: 3600, REP: 5000 }]

        # Fast for shower, dryer, etc
        for th_id in 34,51:
            REP_SETTINGS[ "th%d"%th_id ]  = [ TEMP|{MINR: 60, MAXR: 1800, REP: 50 }, HUMI|{MINR: 60, MAXR: 1800, REP: 500 }]

        # # Generic Sonoff
        # # for th_id in 52,53,54,55,15,8,4,2,1,30,32:
        # for th_id in 55,:
        #     REP_SETTINGS[ "th%d"%th_id ]  = [ TEMP|{MINR: 300, MAXR: 1800, REP: 20 }, HUMI|{MINR: 300, MAXR: 1800, REP: 100 }]

        # for th_id in range( 16,25 ):
        #     REP_SETTINGS[ "th%d"%th_id ]  = [ TEMP|{MINR: 300, MAXR: 1800, REP: 20 }, HUMI|{MINR: 300, MAXR: 1800, REP: 100 }]

        global DEFAULT_REP_TH
        DEFAULT_REP_TH = [ TEMP|{MINR: 300, MAXR: 1800, REP: 20 }, HUMI|{MINR: 300, MAXR: 1800, REP: 100 }]
        # DEFAULT_REP_TH = []
    init1()

    def get_config( short_name, name ):
        pass
        m = re.match( r"(th\d+)", device_id )
        th_id = m.groups()[0]
        return SETTINGS.get( device_id ) or SETTINGS.get( th_id ) or SETTINGS.get( None )

    class Device():
        def __init__( self, dev_info ):
            # fix endpoint key type: str -> int
            dev_info["endpoints"] = { int(k):v for k,v in dev_info["endpoints"].items() }
            self.dev_info = dev_info
            self.clusters = {}
            self.reportings = {}
            for endpoint, d in dev_info["endpoints"].items():
                for cluster in d["clusters"].get("input",()):
                    self.clusters[cluster] = endpoint
                for rep in d["configured_reportings"]:
                    rep[ "endpoint" ] = endpoint
                    self.reportings[ rep["cluster"] ] = rep

            self.friendly_name = dev_info["friendly_name"]

        # add reporting, only if it is not already set
        def add_reporting( self, rep ):
            name = self.friendly_name
            prev = self.reportings.get( rep["cluster"] )
            text = " ".join( [ 
                f" {label:<5}: {prev[key]:>6} -> {rep[key]:>6}" if prev else f" {label:<5}: {'':>6} -> {rep[key]:>6}" 
                for label, key in [
                ("MINR" , "minimum_report_interval"),
                ("MAXR" , "maximum_report_interval"),
                ("REP"  , "reportable_change")
                ]] )
            if prev == rep:
                print( f"{name:<40}: reporting already set for {rep['cluster']:<30}" )
            else:
                print( f"{name:<40}: add reporting for         {rep['cluster']:<30}:  {text}" )
                pub_payload = dict( rep )
                pub_payload["id"] = name
                self.reportings_to_add.append( rep )

        # remove binding, whether it is set ot not (test is done upstream)
        def remove_binding( self, b ):
            name = self.friendly_name
            print( f"{name:<40}: unbind {b}" )
            self.bindings_to_remove.append( b )

        def add_binding( self, b ):
            name = self.friendly_name
            print( f"{name:<40}: bind {b}" )
            self.bindings_to_add.append( b )

        def prep_config( self, mgr ):
            # Extract info from device name
            d = self.dev_info
            name = d["friendly_name"]
            if not( m:= re.match( r"([^\d]+\d+)", name ) ):
                print( f"{name:<40}: filtered out" )
                return
            self.short_name, = m.groups()

            self.reportings_to_add   = []
            self.bindings_to_remove  = []
            self.bindings_to_add     = []
            self.messages_to_publish = []

            # Configure Battery Reporting with a suitable min/max time to ensure
            # periodic reports are frequent enough to prevent devices from becoming
            # flagged as unavailable
            # 
            if d["power_source"].lower() == "battery":
                if endpoint := self.clusters.get( "genPowerCfg" ):
                    #   Set battery reporting for stuff that supports it
                    #
                    if d["model_id"] not in ["LYWSD03MMC-z"]:
                        self.add_reporting({
                            "endpoint"                : endpoint,
                            "attribute"               : "batteryPercentageRemaining",
                            "cluster"                 : "genPowerCfg",
                            "maximum_report_interval" : 7200,
                            "minimum_report_interval" : 3600,
                            "reportable_change"       : 5
                        })

            #   Add configured reportings from REP_SETTINGS
            #
            if settings := REP_SETTINGS.get( self.short_name ):
                for setting in settings:
                    if endpoint := self.clusters.get( setting["cluster"] ):
                        self.add_reporting( setting | {"endpoint":endpoint})
            elif re.match( r"th\d+_", name ):
                if endpoint := self.clusters.get( "msTemperatureMeasurement" ):
                    for setting in DEFAULT_REP_TH:
                        self.add_reporting( setting | {"endpoint":endpoint})

            #   For Xiaomi we must reconfigure reporting otherwise the default drains the battery too fast
            #   But that will result in a Table Full error unless we first unbind something...
            #   Let's unbind genPollCtrl
            coordinator_address = mgr.device_dict["Coordinator"].dev_info["ieee_address"]
            if d["manufacturer"] == "Xiaomi" and d["model_id"] == "LYWSD03MMC-z" and self.reportings_to_add:
                for binding in d["endpoints"][1]["bindings"]:
                    if binding["cluster"] == "genPollCtrl":
                        self.remove_binding( {
                            "from"      : name, 
                            "to"        : "Coordinator", 
                            "clusters"  : ["genPollCtrl"]
                        } )
                self.add_binding( {
                    "from"          : name, 
                    "from_endpoint" : 1, 
                    "to"            : "Coordinator", 
                    "to_endpoint"   : 1,
                    "clusters"      : ["genPollCtrl"]
                    } )

            return bool( self.reportings_to_add or self.bindings_to_remove or self.bindings_to_add )

    class ConfigureZigbeeStuff():
        def __init__( self ):
            # Connect MQTT
            self.mqtt = gmqtt.Client( "conf_zigbee_diy", False  )
            self.mqtt.on_connect    = self.on_connect
            self.mqtt.on_message    = self.on_message
            self.mqtt.set_auth_credentials( config.MQTT_USER, config.MQTT_PASSWORD )

            self.device_dict  = {}
            self.devices_todo = set()

        def on_connect( self, client, flags, rc, properties ):
            print("MQTT CONNECT")
            self.mqtt.subscribe( f"{Z2M_TOPIC}/bridge/devices" )
            self.mqtt.subscribe( f"{Z2M_TOPIC}/bridge/response/device/configure_reporting" )

        async def on_message(self, client, topic, payload, qos, properties):
            #
            ################ List of devices sent by Z2M ################
            #
            if topic == f"{Z2M_TOPIC}/bridge/devices":
                print( "Got device list" )
                self.mqtt.unsubscribe( topic )
                if self.device_dict:
                    return      # only get it once on startup.

                # index device list by friendly_name.
                self.device_dict = {}
                for dev_info in sorted( orjson.loads( payload ), key=lambda d:d["friendly_name"]):
                    name = dev_info[ "friendly_name" ]
                    self.device_dict[ name ] = Device( dev_info )

                # keep only devices we want to reconfigure.
                for dev in self.device_dict.values():
                    name = dev.friendly_name
                    if dev.prep_config( self ):
                        self.devices_todo.add( name )
                        # print( f"{name:<40}: checking availability..." )
                        self.mqtt.subscribe( f"{Z2M_TOPIC}/{name}/availability")

                # We subscribed to availability topic. Process continues when
                # we receive the availability message.
                return

            if topic.startswith( f"{Z2M_TOPIC}/bridge/response/device/" ):
                j = orjson.loads( payload )
                print( "response:", topic, j.get("data",{}).get("id"), j["status"], j.get("error") )
                return


            # Back to processing all messages...
            # Get device_id and subtopic (if any)
            m = re.match( r"z2m/([^/]*)/?(.*)", topic )
            if not m:
                return
            name, subtopic = m.groups()
            j = orjson.loads( payload )

            ########################## Availability ##########################
            # Upon connection we get device availability as retained messages.
            # Use it to filter out unavailable devices and build todolist.
            if subtopic == "availability":
                self.mqtt.unsubscribe( topic )  # no need to keep it
                if j["state"] != "online":
                    print( f"{name:<40}: is offline" )
                    del self.device_dict[ name ]
                    self.devices_todo.remove( name )
                    return
                else:
                    # We will configure this device when it wakes up and
                    # sends a message.
                    self.mqtt.subscribe( f"{Z2M_TOPIC}/{name}")

            ########################## Message received ##########################
            # When device wakes up and sends a message, configure it.
            if subtopic in ("", "action"):
                print( f"{name:<40}: RX message" )
                if name in self.devices_todo:
                    dev = self.device_dict[ name ]

                    if dev.bindings_to_remove:
                        print( f"{name:<40}: publish unbind" )
                        for b in dev.bindings_to_remove:
                            self.mqtt.publish( "z2m/bridge/request/device/unbind" , orjson.dumps( b ))

                    if dev.reportings_to_add:
                        print( f"{name:<40}: publish reportings" )
                        for rep in dev.reportings_to_add:
                            pub_payload = dict( rep )
                            pub_payload["id"] = name
                            self.mqtt.publish( "z2m/bridge/request/device/configure_reporting" , orjson.dumps( pub_payload ))

                    if dev.bindings_to_add:
                        print( f"{name:<40}: publish bind" )
                        for b in dev.bindings_to_add:
                            self.mqtt.publish( "z2m/bridge/request/device/bind" , orjson.dumps( b ))

                    self.devices_todo.remove( name )
                    self.mqtt.unsubscribe( f"{Z2M_TOPIC}/{name}")

            return
            
            # elif m:=re.match( r"z2m/(th[^/]*)", topic ) and device_id in self.devices_todo:
            #     self.devices_todo.remove( device_id )




        def start( self ):
            with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
                runner.run(self.astart())

        async def astart( self ):
            await self.mqtt.connect( config.MQTT_BROKER )
            start_time = time.monotonic()
            try:
                for n in range( 30 ):
                    await asyncio.sleep(1)
                    # print( "TODO:", self.devices_todo )
                print( "Waiting..." )

                while self.devices_todo:
                    try:
                        await asyncio.sleep(10)

                    except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
                        print("Terminated.")
                        return
                    except:
                        log.exception( "Exception" )
                        await asyncio.sleep(1)        

                print("Shutting down in 20s...")
                await asyncio.sleep(20)

            finally:
                await asyncio.sleep(0.1)

    mgr = ConfigureZigbeeStuff()
    mgr.start()


if 0:
    #   Unbind buggy clusters from Tuya door/window sensors
    #

    class UnbindBuggyTuya( MQTTWrapper ):
        def __init__( self ):
            super().__init__( "reporting_interval" )
            self.devices_todo = set()
            self.rx_messages = 0

        def on_connect( self, client, flags, rc, properties ):
            print("MQTT CONNECT")
            self.mqtt.subscribe( "z2m/#" )

        def get_device_id( self, topic ):
            # for match in r"cf\d+", r"th19_", r"th5_":
            for prefix in r"th19_",:
                if m := re.match( rf"z2m/({prefix}[^/]*)", topic ):
                    return m.groups()[0]

        async def on_message(self, client, topic, payload, qos, properties):
            self.rx_messages += 1

            device_id = self.get_device_id( topic )
            if not device_id:
                return

            j = orjson.loads(payload)
            if topic.endswith("/availability"):
                if j["state"] == "online":
                    self.devices_todo.add( device_id )

            elif device_id in self.devices_todo:
                # self.devices_todo.remove( device_id )

                print( "Configuring %s" % (device_id,) )
                pub_payload = {
                        "from"      : device_id, 
                        "to"        : "Coordinator", 
                        "clusters"  : ["genPollCtrl"]
                    }
                self.publish( "z2m/bridge/request/device/unbind" , orjson.dumps( pub_payload ))
                print( "TODO:", self.devices_todo )

        def start( self ):
            with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
                runner.run(self.astart())

        async def astart( self ):
            await self.mqtt.connect( config.MQTT_BROKER )
            start_time = time.monotonic()
            try:
                for n in range( 30 ):
                    await asyncio.sleep(1)
                    print( "TODO:", self.devices_todo, "msg", self.rx_messages )
                print( "Waiting..." )

                while self.devices_todo:
                    try:
                        await asyncio.sleep(10)

                    except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
                        print("Terminated.")
                        return
                    except:
                        log.exception( "Exception" )
                        await asyncio.sleep(1)        
            finally:
                await asyncio.sleep(0.1)

    mgr = UnbindBuggyTuya()
    mgr.start()    