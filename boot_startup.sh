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
screen -dmS pv        bach -c 'python3.11 modbus_mitm.py'
screen -dmS buf       bach -c 'python3.11 mqtt_buffer.py'
screen -dmS chauffage bach -c 'python3.11 chauffage_mqtt.py'
screen -dmS can       bach -c 'python3.11 server_can.py'
screen -dmS fakemeter bach -c 'python3.11 server_fake_meter.py'

# screen -d -m -t ventilation python3.11 ventilation.py
