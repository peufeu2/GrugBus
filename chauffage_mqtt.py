import asyncio, os, time, datetime, logging, sys, uvloop, dateparser, urllib.request, math, orjson
from path import Path

import config
from pv.mqtt_wrapper import MQTTWrapper

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

async def start():
    mqtt = MQTTWrapper( "mqtt_chauffage" )
    await mqtt.mqtt.connect( config.MQTT_BROKER_LOCAL )
    await run( mqtt )

async def run(mqtt ):
    last_line = None
    timeout = None
    while True:
        try:
            await asyncio.sleep( sleep_delay )
            for k,v in get():
                if isinstance( v,float ):
                    v = round( v, 2 )
                if isinstance( v,bool ):
                    v = int(v)
                print(k,v)
                mqtt.publish_value( "chauffage/"+k, v )

            lines = urllib.request.urlopen("http://192.168.0.16/log",timeout=2).read().decode("windows-1252").split("\n")
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                if line == last_line:
                    continue
                last_line = line
                mqtt.mqtt.publish("chauffage/log", line)
        except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
            print("Terminated.")
            return
        except:
            log.exception( "Exception" )
            await asyncio.sleep(1)


with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
    runner.run(start())
