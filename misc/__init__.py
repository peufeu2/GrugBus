#!/usr/bin/python
# -*- coding: utf-8 -*-

import time, asyncio, math, collections

class Metronome:
    """
        Simple class to periodically trigger an event.
        Missed trigger points are ignored.
    """
    def __init__( self, tick ):
        self.tick = tick
        self.next_tick = 0
        self.last_tick = 0

    # set tick period
    def set( self, tick ):
        if tick < self.tick:
            # asking for shorter tick interval: cancel previous tick and replace it with new one
            # so it triggers as soon as it should
            self.next_tick += tick - self.tick
        # if asking for a longer tick interval, let next tick happen with the previous interval
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
    def __init__( self, duration=1, expired=False ):
        self.duration = duration
        self.reset( duration )
        if expired:
            self.expiry = 0.

    def set_duration( self, duration ):
        self.duration = duration

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

class Interval:
    def __init__( self, minimum, maximum, func=float ):
        self._func = func
        self.minimum = func( minimum )
        self.maximum = func( maximum )

    def clip( self, value ):
        return min( self.maximum, max( self.minimum, self._func( value )))

    def set_maximum( self, maximum ):
        self.maximum = maximum

    def set_minimum( self, minimum ):
        self.minimum = minimum

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
class MovingAverageSeconds:
    def __init__( self, time_window ):
        self.queue = collections.deque( )
        self.sum_value = 0.0
        self.sum_time  = 0.0
        self.time_window = time_window
        self.tick = 0
        self.ncalls = 0
        self.is_full = False     # True when we have enough data to fill the time window

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
            self.is_full = True

        self.ncalls += 1    # remove rouding error
        if self.ncalls > 10000:
            self.ncalls = 0
            self.sum_value = sum( value for value, dt in q )
            self.sum_time  = sum( dt    for value, dt in q )

        return self.sum_value / self.sum_time

        # return None if we don't have enough data yet

#
#   Time weighted moving average
#
class MovingAveragePoints:
    def __init__( self, points ):
        self.queue = collections.deque( maxlen=points )
        self.sum_value = 0.0
        self.ncalls = 0
        self.is_full = False

    def append( self, value ):
        a = self.queue
        if len(q) == q.maxlen:
            self.sum_value -= q.popleft()
            self.is_full = True
        self.sum_value += value
        q.append( value )

        # moving average
        self.ncalls += 1    # remove rouding error
        if self.ncalls > 10000:
            self.ncalls = 0
            self.sum_value = sum( q )

        return self.sum_value / len(q)

        # return None if we don't have enough data yet

def average( l ):
    if l:
        return sum(l)/len(l)
    else:
        return 0


# class RingBuffer:
#     def __init__( self, length ):
#         self.q = [0] * length
#         self.pos = 0

#     def cur( self ):
#         return self.q[self.pos]

#     def at( self, offset ):
#         return self.q[ (self.pos+offset) % len(self.q) ]

#     def set( self, offset, value ):
#         self.q[ (self.pos+offset) % len(self.q) ] = value

#     def pop( self ):
#         r = self.q[self.pos]
#         self.q[self.pos] = 0
#         self.pos = (self.pos+1) % len(self.q)
#         return r

#     def add( self, offset, value ):
#         self.q[ (self.pos+offset) % len(self.q) ] += value


if __name__ == "__main__":
    m = MovingAverage( 0.5 )
    for n in range( 100 ):
        print( n, m.append( n ))
        time.sleep( 0.1 )