#!/usr/bin/python
# -*- coding: utf-8 -*-

MQTT_USER       = "peufeu"
MQTT_PASSWORD   = "lmao"
MQTT_BROKER     = "solarpi"
CLICKHOUSE_USER = "default"
CLICKHOUSE_PASSWORD = "shush"

# pool mqtt data and bulk insert into database every ... seconds
CLICKHOUSE_INSERT_PERIOD_SECONDS = 5

# Modbus configuration
# COM_PORT_SOLIS      = "COM4"
# COM_PORT_FAKE_METER = "COM5"
# COM_PORT_METER      = "COM6"
# COM_PORT_METER_SOLIS = "COM7"
#
#   Use by-id so the ports don't move around after a plug and pray session
#
COM_PORT_SOLIS       = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A10NBG8C-if00-port0"
COM_PORT_FAKE_METER  = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_54D2042115-if00"
COM_PORT_METER       = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_54D2042261-if00"
COM_PORT_METER_SOLIS = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_54D2042257-if00"

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

