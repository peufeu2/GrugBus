Pi Setup

Setup fstab to automount NAS with the code on it
Setup launch of scripts at boot (see boot_crontab.txt)

Install python >= 3.11
Install pymodbus and other libraries with pip

pyserial-asyncio
orjson
uvloop

Some of these are probably not necessary:
aiohttp               3.8.3
aiomqtt               2.3.0
aiosignal             1.3.1
async-timeout         4.0.2
attrs                 22.2.0
charset-normalizer    2.1.1
dateparser            1.1.7
frozenlist            1.3.3
gmqtt                 0.6.16
gs-usb                0.3.0
idna                  3.4
msgpack               1.0.8
multidict             6.0.4
OPi.GPIO              0.5.2
orjson                3.8.5
path                  16.6.0
pymodbus              3.7.3
pyserial              3.5
pyserial-asyncio      0.6
python-can            4.4.2
python-dateutil       2.8.2
uvloop                0.20.0
xopen                 1.7.0



# UART

add to /boot/armbianEnv.txt
overlays=uart1

# GPIO config

sudo pip3.11 install --upgrade OPi.GPIO

# Execute as root:
groupadd gpio
usermod -aG gpio peufeu

# Execute as root (put boot_startup_root.sh in root crontab @reboot)
chgrp gpio /sys/class/gpio/export
chgrp gpio /sys/class/gpio/unexport
chmod 775 /sys/class/gpio/export
chmod 775 /sys/class/gpio/unexport
echo "201" > /sys/class/gpio/export
chgrp -HR gpio /sys/class/gpio/gpio201/
chmod -R 775 /sys/class/gpio/gpio201

# edit OPi source code

                if _gpio_warnings:
                    warnings.warn("Channel {0} is already in use, continuing anyway. Use GPIO.setwarnings(False) to disable warnings.".format(channel), stacklevel=2)
                sysfs.unexport(pin)
                sysfs.export(pin)
 
 comment the last 2 lines to prevent unexport + export, which resets permissions set at the previous step



