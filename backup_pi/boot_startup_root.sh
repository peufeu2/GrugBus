#! /bin/bash

# set permissions for Pico RESET GPIO
chgrp gpio /sys/class/gpio/export
chgrp gpio /sys/class/gpio/unexport
chmod 775 /sys/class/gpio/export
chmod 775 /sys/class/gpio/unexport
echo "201" > /sys/class/gpio/export
chgrp -HR gpio /sys/class/gpio/gpio201/
chmod -R 775 /sys/class/gpio/gpio201

