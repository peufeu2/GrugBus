#!/usr/bin/python
# -*- coding: utf-8 -*-

import time, asyncio, math

class Metronome():
    """
        Simple class to periodically trigger an event
    """
    def __init__( self, tick ):
        try:    tick, base = tick
        except: base = 0
        self.tick = tick
        self.next_tick = base

    def reset( self ):
        self.next_tick = time.monotonic()+self.tick

    async def wait( self ):
        ct = time.monotonic()
        if self.next_tick < ct:
            self.next_tick += self.tick * math.ceil((ct-self.next_tick)/self.tick)
        delay = self.next_tick - ct
        if delay>0.01:
            await asyncio.sleep(delay)

    def ticked( self ):
        ct = time.monotonic()
        if self.next_tick < ct:
            self.next_tick += self.tick * math.ceil((ct-self.next_tick)/self.tick)
            return True

class Timeout():
    """
        Simple class to periodically trigger an event
    """
    def __init__( self, duration, expired=False ):
        self.duration = duration
        self.reset( duration )
        if expired:
            self.expiry = 0

    def reset( self, duration=None ):
        self.expiry   = time.monotonic() + (duration or self.duration)

    def reset_or_extend( self, duration ):
        self.expiry   = max( self.expiry, time.monotonic() + duration )

    def expired( self ):
        return time.monotonic() > self.expiry

    def remain( self ):
        return max(0, self.expiry - time.monotonic())

def interpolate( xa, ya, xb, yb, x ):
    if x <= xa:
        return ya
    elif x >= xb:
        return yb
    else:
        return ya + (yb-ya)*(x-xa)/(xb-xa)

def average( l ):
    if l:
        return sum(l)/len(l)
    else:
        return 0