#! /bin/bash

#
#	This executes at boot on solarpi, which also runs the MQTT broker.
#

# get latest code
mount -a
cd /home/peufeu/solaire
./copy.sh

sleep 5

# run solar management
screen -dmS buf       bash -c 'python3.11 mqtt_buffer.py'
screen -dmS chauffage bash -c 'python3.11 chauffage_mqtt.py'
screen -dmS can       bash -c 'python3.11 pv_can.py'
screen -dmS fakemeter bash -c 'python3.11 pv_controller.py'
screen -dmS router    bash -c 'python3.11 pv_router.py'

# screen -d -m -t ventilation python3.11 ventilation.py
