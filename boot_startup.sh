#! /bin/bash

#
#	This executes at boot on solarpi, which also runs the MQTT broker.
#

# get latest code
mount -a
cd /home/peufeu/solaire
./copy.sh

# run solar management
screen -d -m -t pv python3.11 modbus_mitm.py

# log MQTT to compressed files
screen -d -m -t buf python3.11 mqtt_buffer.py

screen -d -m -t chauffage python3.11 chauffage_mqtt.py

# screen -d -m -t ventilation python3.11 ventilation.py
