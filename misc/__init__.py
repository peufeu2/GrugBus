#!/usr/bin/python
# -*- coding: utf-8 -*-

import time, asyncio, math, collections

class Metronome:
    """
        Simple class to periodically trigger an event
    """
    def __init__( self, tick ):
        try:    tick, base = tick
        except: base = 0
        self.tick = tick
        self.next_tick = base

    def set( self, tick ):
        if self.tick != tick:
            # cancel previous tick and replace it with new one
            self.next_tick += tick - self.tick
            self.tick = tick

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

class Chrono:
    def __init__( self ):
        self.tick = time.monotonic()

    def lap( self ):
        t = time.monotonic()
        dt = t-self.tick
        self.tick=t
        return dt

class Timeout:
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

    def at_least( self, duration ):
        self.expiry   = max( self.expiry, time.monotonic() + duration )

    def at_most( self, duration ):
        self.expiry   = min( self.expiry, time.monotonic() + duration )

    def expired( self ):
        return time.monotonic() > self.expiry

    def remain( self ):
        return max(0, self.expiry - time.monotonic())

class BoundedCounter:
    def __init__( self, value, minimum, maximum ):
        self.value   = value
        self.minimum = minimum
        self.maximum = maximum

    def add( self, increment ):
        self.value = min( self.maximum, max( self.minimum, self.value + increment ))
        return self.value 

#
#   Time weighted moving average
#
class MovingAverage:
    def __init__( self, time_window ):
        self.queue = collections.deque( )
        self.sum_value = 0.0
        self.sum_time  = 0.0
        self.time_window = time_window
        self.tick = 0

    def append( self, value ):
        # time since last append
        t = time.monotonic()
        dt = t-self.tick
        if not self.tick:
            # on first call, ignore value and just keep the timestamp
            self.tick=t
            return
        self.tick=t

        # add to total
        value *= dt
        self.sum_value += value
        self.sum_time += dt
        q = self.queue
        q.append( (value,dt) )

        # moving average
        if self.sum_time >= self.time_window:
            old_value, old_dt = q.popleft()
            self.sum_value -= old_value
            self.sum_time -= old_dt
            return self.sum_value / self.sum_time

        # return None if we don't have enough data yet


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


if __name__ == "__main__":
    m = MovingAverage( 0.5 )
    for n in range( 100 ):
        print( n, m.append( n ))
        time.sleep( 0.1 )