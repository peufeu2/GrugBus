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
import time, asyncio, math

class Metronome():
    """
        Simple class to periodically trigger an event
    """
    def __init__( self, tick ):
        self.tick = tick
        self.next_tick = 0

    async def wait( self ):
        ct = time.time()
        if self.next_tick < ct:
            self.next_tick += self.tick * math.ceil((ct-self.next_tick)/self.tick)
        delay = self.next_tick - ct
        if delay>0.01:
            await asyncio.sleep(delay)
