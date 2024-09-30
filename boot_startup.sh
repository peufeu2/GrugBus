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
screen -dmS pv        bash -c 'python3.11 modbus_mitm.py'
screen -dmS buf       bash -c 'python3.11 mqtt_buffer.py'
screen -dmS chauffage bash -c 'python3.11 chauffage_mqtt.py'
screen -dmS can       bash -c 'python3.11 server_can.py'
screen -dmS fakemeter bash -c 'python3.11 server_fake_meter.py'

# screen -d -m -t ventilation python3.11 ventilation.py
