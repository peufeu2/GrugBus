#!/usr/bin/python
# -*- coding: utf-8 -*-

# MQTT
MQTT_USER       = "peufeu"
MQTT_PASSWORD   = "lmao"
MQTT_BROKER     = "192.168.0.27"

MQTT_BUFFER_PATH = "/home/peufeu/mqtt_buffer"

# Clickhouse
CLICKHOUSE_USER = "default"
CLICKHOUSE_PASSWORD = "shush"
CLICKHOUSE_INSERT_PERIOD_SECONDS = 5    # pool mqtt data and bulk insert into database every ... seconds

#   Modbus configuration
#   Use by-id so the ports don't move around after a plug and pray session
#
COM_PORT_METER       = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_54D2042112-if00"   # Main meter
COM_PORT_SOLIS       = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_54D2042115-if00"   # Local meters and inverters

COM_PORT_FAKE_METER1 = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_54D2042257-if00"   # Solis1 fakemeter
COM_PORT_FAKE_METER2 = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A10NBG8C-if00-port0" # Solis2 fakemeter

# not used
# COM_PORT_METER       = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_54D2042261-if00"

# How often we modbus these devices
POLL_PERIOD_SOLIS    = 2
POLL_PERIOD_METER    = 0.1
POLL_PERIOD_FRONIUS  = 2
POLL_PERIOD_SOLIS_METER = 0.5

# Solis is a bit deaf, sometimes it needs repeating
MODBUS_RETRIES_SOLIS = 3
MODBUS_RETRIES_METER = 1

# Inverter auto turn on/off settings
SOLIS_TURNOFF_BATTERY_SOC  = 20
SOLIS_TURNOFF_MPPT_VOLTAGE = 30
SOLIS_TURNON_MPPT_VOLTAGE  = 80

# This overwrites some of the above parameters, like passwords.
# You have to create this file yourself.
from config_secret import *

