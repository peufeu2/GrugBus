#!/usr/bin/python
# -*- coding: utf-8 -*-

import time, asyncio, math

class Metronome():
    """
        Simple class to periodically trigger an event
    """
    def __init__( self, tick ):
        self.tick = tick
        self.next_tick = 0

    def reset( self ):
        self.next_tick = time.time()+self.tick

    async def wait( self ):
        ct = time.time()
        if self.next_tick < ct:
            self.next_tick += self.tick * math.ceil((ct-self.next_tick)/self.tick)
        delay = self.next_tick - ct
        if delay>0.01:
            await asyncio.sleep(delay)

    def ticked( self ):
        ct = time.time()
        if self.next_tick < ct:
            self.next_tick += self.tick * math.ceil((ct-self.next_tick)/self.tick)
            return True

class Timeout():
    """
        Simple class to periodically trigger an event
    """
    def __init__( self, duration, expired=False ):
        self.reset( duration )
        if expired:
            self.expiry = 0

    def reset( self, duration=None ):
        self.duration = duration or self.duration
        self.expiry   = time.time() + self.duration

    def expired( self ):
        return time.time() > self.expiry

    def remain( self ):
        return max(0, self.expiry - time.time())

def interpolate( xa, ya, xb, yb, x ):
    if x <= xa:
        return ya
    elif x >= xb:
        return yb
    else:
        return ya + (yb-ya)*(x-xa)/(xb-xa)
