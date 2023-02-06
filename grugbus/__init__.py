#!/usr/bin/python
# -*- coding: utf-8 -*-


"""
    GrugBus brings modbus up to modern Neanderthal tech level, including:

        -   No need remember number, address, offset, function code, etc, just read()
        -   Bulk register reads up to 40x faster than single reads
        -   Automated register list generation from datasheet PDF tables copypasted into spreadsheet


"""

# import grugbus.register
from . import registers
from .device import SlaveDevice, DeviceBase, LocalServer
