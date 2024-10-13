#! /bin/bash

mount -a
cp -rvau /media/apollo14/peufeu/dev/arduino/solaire/*.py /home/peufeu/solaire
cp -rvau /media/apollo14/peufeu/dev/arduino/solaire/*.sh /home/peufeu/solaire
cp -rvau /media/apollo14/peufeu/dev/arduino/solaire/{grugbus,misc,pv,pico} /home/peufeu/solaire
cd /home/peufeu/solaire
