#!/usr/bin/python
# -*- coding: utf-8 -*-

import time, asyncio, math, collections

class Metronome:
    """
        Simple class to periodically trigger an event.
        Missed trigger points are ignored.
    """
    def __init__( self, tick ):
        try:    tick, base = tick
        except: base = 0
        self.tick = tick
        self.next_tick = base%tick
        self.last_tick = 0

    # set tick period
    def set( self, tick ):
        if self.tick != tick:
            # cancel previous tick and replace it with new one
            self.next_tick += tick - self.tick
            self.tick = tick

    # realign ticks to current time
    def reset( self ):
        self.next_tick = time.monotonic()+self.tick

    # Wait for next tick. First call never waits.
    async def wait( self ):
        if self.last_tick:
            ct = time.monotonic()
            if self.next_tick < ct:
                self.next_tick += self.tick * math.ceil((ct-self.next_tick)/self.tick)
            delay = self.next_tick - ct
            if delay>0.01:
                await asyncio.sleep(delay)
        self.last_tick = time.monotonic()

    def ticked( self ):
        ct = time.monotonic()
        if self.next_tick < ct:
            self.next_tick += self.tick * math.ceil((ct-self.next_tick)/self.tick)
            lt = self.last_tick
            self.last_tick = ct
            return ct - lt
        return 0

class Chrono:
    def __init__( self ):
        self.reset()

    def reset( self ):
        self.tick = time.monotonic()

    def elapsed( self ):
        return time.monotonic() - self.tick

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
            self.expiry = 0.

    def reset( self, duration=None ):
        self.start_time = st = time.monotonic()
        self.expiry     = st + (duration or self.duration)

    def at_least( self, duration ):
        self.expiry   = max( self.expiry, time.monotonic() + duration )

    def at_most( self, duration ):
        self.expiry   = min( self.expiry, time.monotonic() + duration )

    def expire( self ):
        self.expiry = 0.

    def expired( self ):
        if time.monotonic() > self.expiry:
            self.expiry = 0. # remember expired() was called and returned True
            return True
        return False

    # Returns True once after the timeout has expired, then False on
    # subsequent calls. Useful for one-shots.
    def expired_once( self ):
        return self.expiry and self.expired()

    def remain( self ):
        return max(0, self.expiry - time.monotonic())

    def elapsed( self ):
        return time.monotonic() - self.start_time

class BoundedCounter:
    def __init__( self, value, minimum, maximum, func=float ):
        self._func = func
        self.minimum = func( minimum )
        self.maximum = func( maximum )
        self.set(value)

    def set( self, value ):
        self.value = min( self.maximum, max( self.minimum, self._func( value )))
        return self.value

    def clip( self, value ):
        return min( self.maximum, max( self.minimum, self._func( value )))

    def set_maximum( self, maximum ):
        self.maximum = maximum
        self.set( self.value )

    def set_minimum( self, minimum ):
        self.minimum = minimum
        self.set( self.value )

    def to_maximum( self ):
        self.value = self.maximum

    def to_minimum( self ):
        self.value = self.minimum

    def at_maximum( self ):
        return self.value == self.maximum

    def at_minimum( self ):
        return self.value == self.minimum

    def add( self, increment ):
        return self.set( self.pretend_add( increment ))

    def addsub( self, cond, value ):
        self.add( value if cond else -value )

    def pretend_add( self, increment ):
        return min( self.maximum, max( self.minimum, self._func( self.value + increment )))


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
        self.ncalls = 0

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
        while len(q)>1 and self.sum_time >= self.time_window:
            old_value, old_dt = q.popleft()
            self.sum_value -= old_value
            self.sum_time  -= old_dt

        self.ncalls += 1    # remove rouding error
        if self.ncalls > 10:
            self.ncalls = 0
            self.sum_value = sum( value for value, dt in q )
            self.sum_time  = sum( dt    for value, dt in q )

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