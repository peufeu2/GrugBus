#! /bin/bash

#
#	This executes at boot on server.
#
date -Is
export BOKEH_ALLOW_WS_ORIGIN="192.168.0.1:5001,localhost:5001,192.168.0.1:5002,localhost:5002"
echo "Startup..."
which python3.11
cd /home/peufeu/dev/arduino/solaire
pwd
screen -dmS bokehs python3.11 bokehplot.py
screen -dmS click  python3.11 mqtt_clickhouse.py
echo "Done."
