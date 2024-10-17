#! /bin/bash

#
#	This executes at boot on solarpi, which also runs the MQTT broker.
#

# get latest code
mount -a
cd /home/peufeu/solaire
./copy.sh

sleep 1

# run solar management
screen -dmS buf       bash -c 'python3.11 mqtt_buffer.py ; exec bash'
screen -dmS chauffage bash -c 'python3.11 chauffage_mqtt.py ; exec bash'
screen -dmS can       bash -c 'python3.11 pv_can.py ; exec bash'
screen -dmS fakemeter bash -c 'python3.11 pv_controller.py ; exec bash'
screen -dmS router    bash -c 'python3.11 pv_router.py ; exec bash'
screen -dmS mainboard bash -c 'python3.11 pv_mainboard.py upload ; exec bash'

# screen -d -m -t ventilation python3.11 ventilation.py
