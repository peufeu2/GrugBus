import asyncio, os, time, datetime, traceback, logging, sys, uvloop, dateparser, urllib.request, pprint, math
import orjson, socket
from gmqtt import Client as MQTTClient
from path import Path
from xopen import xopen
import config
from path import Path

logging.basicConfig( #encoding='utf-8', 
                     level=logging.INFO,
                     format='[%(asctime)s] %(levelname)s:%(message)s',
                     handlers=[logging.FileHandler(filename=Path(__file__).stem+'.log'), 
                            logging.StreamHandler(stream=sys.stdout)])
log = logging.getLogger(__name__)

RELAYS = {
    "CHE_PF"                   : 0,
    "CHE_PC"                   : 1,
    "CIRCULATEUR_SDE_PC"       : 2,
    "CIRCULATEUR_ETAGE"        : 3,
    "4"                        : 4,
    "CIRCULATEUR_PC"           : 5,
    "CIRCULATEUR_DEPART"       : 6,
    "DEMANDE_PAC"              : 7,
    "CIRCULATEUR_PF"           : 8,
    "VALVE_POWER"              : 9,
    "VALVE_PF_LOOP"            : 10,
    "VALVE_PF_HOT"             : 11,
    "VALVE_ETAGE_BUREAU"       : 12,
    "CIRCULATEUR_BOIS"         : 13,
    "VALVE_PCBT_PC"            : 14,
    "VALVE_PCBT_ETAGE"         : 15,
}


########################################################################################
#   MQTT
########################################################################################
class MQTT():
    def __init__( self ):
        self.mqtt = MQTTClient("chauffage")
        self.mqtt.on_connect    = self.on_connect
        self.mqtt.on_message    = self.on_message
        self.mqtt.on_disconnect = self.on_disconnect
        self.mqtt.on_subscribe  = self.on_subscribe
        self.mqtt.set_auth_credentials( config.MQTT_USER, config.MQTT_PASSWORD )
        self.published_data = {}

    def on_connect(self, client, flags, rc, properties):
        pass

    def on_disconnect(self, client, packet, exc=None):
        pass

    async def on_message(self, client, topic, payload, qos, properties):
        pass

    def on_subscribe(self, client, mid, qos, properties):
        print('MQTT SUBSCRIBED')

    def publish( self, prefix, data, add_heartbeat=False ):
        if not self.mqtt.is_connected:
            log.error( "Trying to publish %s on unconnected MQTT" % prefix )
            return

        t = time.time()
        to_publish = {}

        #   do not publish duplicate data
        for k,v in data.items():
            k = prefix+k
            p = self.published_data.get(k)
            if p:
                if p[0] == v and t<p[1]:
                    continue
            self.published_data[k] = v,t+60 # set timeout to only publish constant data every N seconds
            to_publish[k] = v

        for k,v in to_publish.items():
            self.mqtt.publish( k, str(v), qos=0 )

mqtt = MQTT()

def valid_temp( t ):
    if t>-120:
        return t
    else:
        return math.nan

def get():
    global sleep_delay
    data = urllib.request.urlopen("http://192.168.0.16/status.json?clear", timeout=2).read().decode("utf-8")
    try:
        data = orjson.loads(data)
    except Exception as e:
        print("Error getting data", e)
        print(data)
        raise

    if data["boot_ts"]:
        yield "boot_ts", datetime.datetime.fromisoformat( data["boot_ts"] ).timestamp()
    yield "build_ts", dateparser.parse(data["build_ts"]).timestamp()
    yield "device_ts", data["time"]
    for k,v in data["temperatures"].items():
        yield k, valid_temp( float(v) )
    for k,v in data["inputs"].items():
        yield k, int(v)
    yield "pf_consigne_retour_max",  data["pcbt_pf"]["consigne_retour"]["max"]
    yield "pf_consigne_retour_min",  data["pcbt_pf"]["consigne_retour"]["min"]
    yield "pf_consigne_ambient_max", data["pcbt_pf"]["consigne_ambient"]["max"]
    yield "pf_consigne_ambient_min", data["pcbt_pf"]["consigne_ambient"]["min"]

    yield "pc_consigne_retour_max",  data["pcbt_pc"]["consigne_retour"]["max"]
    yield "pc_consigne_retour_min",  data["pcbt_pc"]["consigne_retour"]["min"]
    yield "pc_consigne_ambient_max", data["pcbt_pc"]["consigne_ambient"]["max"]
    yield "pc_consigne_ambient_min", data["pcbt_pc"]["consigne_ambient"]["min"]

    yield "et_consigne_retour_max",  data["pcbt_et"]["consigne_retour"]["max"]
    yield "et_consigne_retour_min",  data["pcbt_et"]["consigne_retour"]["min"]
    yield "et_consigne_ambient_max", data["pcbt_et"]["consigne_ambient"]["max"]
    yield "et_consigne_ambient_min", data["pcbt_et"]["consigne_ambient"]["min"]

    yield "et_bureau_consigne_ambient_max", data["et_bureau"]["consigne_ambient"]["max"]
    yield "et_bureau_consigne_ambient_min", data["et_bureau"]["consigne_ambient"]["min"]

    # yield "cuisine_consigne_ambient_max", data["demande"]["consigne_ambient"]["max"]
    # yield "cuisine_consigne_ambient_min", data["demande"]["consigne_ambient"]["min"]

    # yield "pv_day_energy", data["pv"]["day_energy"]
    yield "autoconso_fake_power", data["autoconso"]["fake_power"]
    # yield "pv_power", data["pv"]["power"]
    yield "ram", data["ram"]
    yield "ram_blk", data["ram_blk"]
    # yield "relays", sum( 1<<n for n in data["relays"] )
    yield "degommage_state", data["idle"]["d_state"]
    yield "onewire_errors", data["onewire_errors"]
    yield "onewire_reads", data["onewire_reads"]

    for relay_name, relay_number in RELAYS.items():
        yield ("relays/"+relay_name), relay_number in data["relays"]
    for valve_name in ( "PF_LOOP",
                        "PF_HOT"      ,
                        "ETAGE_BUREAU",
                        "PCBT_PC"     ,
                        "PCBT_ETAGE"  ):
        yield ("valves/"+valve_name), RELAYS["VALVE_"+valve_name] in data["valves"]


    # yield "millis", data["millis"]
    # for relay,state,timeout in data["relays_timeouts"]:
        # if relay == 4:
            # yield "r4_timeout", timeout

sleep_delay = 5.0

async def run():
    await mqtt.mqtt.connect( config.MQTT_BROKER_LOCAL )
    last_line = None
    timeout = None
    while True:
        try:
            data = {}
            for k,v in get():
                if isinstance( v,float ):
                    v = round( v, 2 )
                if isinstance( v,bool ):
                    v = int(v)
                print(k,v)
                data[k]=v
            # pprint.pprint(data)
            mqtt.publish( "chauffage/", data )

            lines = urllib.request.urlopen("http://192.168.0.16/log",timeout=2).read().decode("windows-1252").split("\n")
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                if line == last_line:
                    continue
                last_line = line
                mqtt.mqtt.publish("chauffage/log", line)
        except KeyboardInterrupt:
            return
        except:
            log.error( "%s", traceback.format_exc() )

        await asyncio.sleep( sleep_delay )


        


if sys.version_info >= (3, 11):
    with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
    # with asyncio.Runner() as runner:
        runner.run(run())
else:
    uvloop.install()
    asyncio.run(run())
