#!/usr/bin/python
# -*- coding: utf-8 -*-

import os, pprint, time, sys, serial, socket, traceback, struct, datetime, logging, math, traceback, shutil, collections
from path import Path

# This program is supposed to run on a potato (Allwinner H3 SoC) and uses async/await,
# so import the fast async library uvloop
import uvloop
import asyncio, signal, aiohttp

# Modbus
import pymodbus
from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient
from pymodbus.datastore import ModbusServerContext, ModbusSlaveContext, ModbusSequentialDataBlock
from pymodbus.server import StartAsyncSerialServer
from pymodbus.transaction import ModbusRtuFramer

# MQTT
from gmqtt import Client as MQTTClient

# Device wrappers and misc local libraries
from misc import *
import grugbus
from grugbus.devices import Eastron_SDM120, Solis_S5_EH1P_6K_2020_Extras, Eastron_SDM630, Acrel_1_Phase, EVSE_ABB_Terra, Acrel_ACR10RD16TE4
import config

"""
    python3.11
    pymodbus 3.1

TODO: move init after event loop is created
https://github.com/pymodbus-dev/pymodbus/issues/2102


#### WARNING ####
This file is both the example code and the manual.
==================================================

RS485 ports
- Master:       Solis inverters COM port + Meter attached to inverter
- Slave:        Inverter 1 fake meter
- Slave:        Inverter 2 fake meter
- Master:       Main smartmeter (gets its own interface cause it has to be fast, for power routing)

MQTT topics:
pv/                 Everything photovoltaic
pv/solis1/          Inverter 1
pv/solis1/meter     Meter attached to inverter1 (because its internal grid port power measurement sucks too much)
pv/meter/           Meter for the whole house

Reminder:
git remote set-url origin https://<token>@github.com/peufeu2/GrugBus.git

How to prevent USB serial ports from changing names: use /dev/serial/by-id/ names, not /dev/ttywhoknows

#####   Sign of power
When power can flow one way, always positive (example: PV power)
When Power can flow both ways, positive sign is always consumed power
  If something_power is positive, that something is drawing power or current
  If something_power is negative, that something is producing power
This is sometimes not intuitive, but at least it's the same convention everywhere!
    Inverter grid port power 
        positive if it is consuming power (to charge batteries for example)
        negative means it's producing
    Battery power is positive if it is charging
    Backup port power is power consumed by backup loads, always positive
    Grid side meter is positive if the house is consuming power
    etc
"""

# pymodbus.pymodbus_apply_logging_config( logging.DEBUG )
logging.basicConfig( encoding='utf-8', 
                     level=logging.INFO,
                     format='[%(asctime)s] %(levelname)s:%(message)s',
                     handlers=[logging.FileHandler(filename=Path(__file__).stem+'.log'), 
                            logging.StreamHandler(stream=sys.stdout)])
log = logging.getLogger(__name__)

# set max reconnect wait time for Fronius
# pymodbus.constants.Defaults.ReconnectDelayMax = 60000   # in milliseconds


#
#   Helper to set modbus address of a SDM120 smartmeter during installation:
#       
#       It has default address 1, so use this function to change it.
#
if 0:
    async def set_sdm120_address( new_address=4 ):
        d = grugbus.SlaveDevice( 
                AsyncModbusSerialClient(
                    port            = "COM7",
                    timeout         = 0.3,
                    retries         = 2,
                    baudrate        = 9600,
                    bytesize        = 8,
                    parity          = "N",
                    stopbits        = 1,
                    # framer=pymodbus.ModbusRtuFramer,
                ),
                1,          # Modbus address
                "meter", "SDM630 Smartmeter", 
                Eastron_SDM120.MakeRegisters() )
        await d.modbus.connect()

        print("Checking meter on address %s" % d.bus_address )
        await d.rwr_modbus_node_address.read()
        print( "current address", d.rwr_modbus_node_address.value )

        input( "Long press button on meter until display --SET-- to enable modbus writes (otherwise it is protected) and press ENTER" )
        d.rwr_modbus_node_address.value = new_address
        await d.rwr_modbus_node_address.write()

        print( "Write done, if it was successful the meter should not respond to the old address and this should raise a Timeout...")

        await d.rwr_modbus_node_address.read()
        print( "current address", d.rwr_modbus_node_address.value )

    asyncio.run( set_sdm120_address() )
    stop