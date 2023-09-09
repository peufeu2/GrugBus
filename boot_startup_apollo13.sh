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
screen -d -m -S bokehs python3.11 bokehplot.py
screen -d -m -S bokehc python3.11 bokehplot_chauffage.py
screen -d -m -S click  python3.11 mqtt_clickhouse.py
echo "Done."
