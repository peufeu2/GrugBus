import machine, time, sys, os
# import rp2
# from micropython import const
# from rp2 import PIO, asm_pio

# Inputs
PIN_BUTTON    = machine.Pin( 2 , machine.Pin.IN )
PIN_TACH1 = machine.Pin( 9 , machine.Pin.IN , machine.Pin.PULL_UP )
PIN_TACH2 = machine.Pin( 7 , machine.Pin.IN , machine.Pin.PULL_UP )
PIN_TACH3 = machine.Pin( 5 , machine.Pin.IN , machine.Pin.PULL_UP )
PIN_TACH4 = machine.Pin( 3 , machine.Pin.IN , machine.Pin.PULL_UP )

# Outputs
PIN_BUZZER    = machine.Pin( 11, machine.Pin.OUT, value=0 )
PIN_FAN_PWM1  = machine.Pin( 10, machine.Pin.OUT, value=1 )
PIN_FAN_PWM2  = machine.Pin( 8 , machine.Pin.OUT, value=1 )
PIN_FAN_PWM3  = machine.Pin( 6 , machine.Pin.OUT, value=1 )
PIN_FAN_PWM4  = machine.Pin( 4 , machine.Pin.OUT, value=1 )

PIN_RELAY1    = machine.Pin( 15, machine.Pin.OUT, value=0 )
PIN_RELAY2    = machine.Pin( 16, machine.Pin.OUT, value=0 )

PIN_LED5      = machine.Pin( 12, machine.Pin.OUT, value=0 )
PIN_LED7      = machine.Pin( 13, machine.Pin.OUT, value=0 )
PIN_LED9      = machine.Pin( 14, machine.Pin.OUT, value=0 )
PIN_LED8      = machine.Pin( 17, machine.Pin.OUT, value=0 )
PIN_LED6      = machine.Pin( 18, machine.Pin.OUT, value=0 )
PIN_LED4      = machine.Pin( 19, machine.Pin.OUT, value=0 )
PIN_LED3      = machine.Pin( 20, machine.Pin.OUT, value=0 )
PIN_LED2      = machine.Pin( 21, machine.Pin.OUT, value=0 )
PIN_LED1      = machine.Pin( 22, machine.Pin.OUT, value=0 )

# Beep
BUZZER_PWM = machine.PWM( PIN_BUZZER, freq=440, duty_u16=0 )

# Fan PWM output
FANS = [ PIN_FAN_PWM1, PIN_FAN_PWM2, PIN_FAN_PWM3, PIN_FAN_PWM4 ]

# Start fans in OFF state
FAN_PWM = [ machine.PWM(pin, freq=25000, duty_u16=65535) for pin in FANS ]

# 9 pixel display
LED_PINS = [ PIN_LED1, PIN_LED2, PIN_LED3, PIN_LED4, PIN_LED5, PIN_LED6, PIN_LED7, PIN_LED8, PIN_LED9 ]

class Led:
    def __init__( self, pin ):
        self.pin = pin
        self.set( 0 )

    def update( self ):
        self.pin( self.counter != 0 )
        if self.counter > 0:
            self.counter -= 1

    def set( self, on ):
        self.counter = on
        self.pin( on != 0 )

LEDS = [ Led( pin ) for pin in LED_PINS ]
LED_MODE = "fan"

def update_leds( t ):
    if not LED_MODE:
        for led in LEDS:
            led.update()

LEDtimer = machine.Timer()
LEDtimer.init( mode=machine.Timer.PERIODIC, callback=update_leds, freq=20 ) 

# Tachometer
# Frequency us very low, we can just use python counters!
TACHS = [ PIN_TACH1, PIN_TACH2, PIN_TACH3, PIN_TACH4 ]
TACH_COUNTERS = [0,0,0,0]

def cb_tach1(p):
    TACH_COUNTERS[0] += 1
    if LED_MODE == "fan":
        LEDS[0].set( TACH_COUNTERS[0] & 2 )

def cb_tach2(p):
    TACH_COUNTERS[1] += 1
    if LED_MODE == "fan":
        LEDS[3].set( TACH_COUNTERS[1] & 2 )

def cb_tach3(p):
    TACH_COUNTERS[2] += 1
    if LED_MODE == "fan":
        LEDS[2].set( TACH_COUNTERS[2] & 2 )

def cb_tach4(p):
    TACH_COUNTERS[3] += 1
    if LED_MODE == "fan":
        LEDS[5].set( TACH_COUNTERS[3] & 2 )

PIN_TACH1.irq( trigger=machine.Pin.IRQ_RISING, handler=cb_tach1 )
PIN_TACH2.irq( trigger=machine.Pin.IRQ_RISING, handler=cb_tach2 )
PIN_TACH3.irq( trigger=machine.Pin.IRQ_RISING, handler=cb_tach3 )
PIN_TACH4.irq( trigger=machine.Pin.IRQ_RISING, handler=cb_tach4 )

# UART
PIN_UART0_TX = machine.Pin( 0 )
PIN_UART0_RX = machine.Pin( 1 )
UART0 = machine.UART(0, 112500)
UART0.init(
    baudrate  = 112500,
    bits      = 8,
    parity    = None,
    stop      = 1,
    rx        = PIN_UART0_RX,
    tx        = PIN_UART0_TX,
    txbuf     = 4096,           # bytes
    rxbuf     = 4096,           # bytes
    flow      = 0,              # no flow control
    timeout   = 1000,            # timeout in ms
    )

def send( s ):
    UART0.write( s.encode() )

def getline():
    s = UART0.readline()
    if s == None:
        return s
    else:
        return s.decode()

def cmd_relays( args ):
    if len(args) != 2:
        send("!relays: needs 2 ints 0-1")
        return
    PIN_RELAY1( int( args[0] ))
    PIN_RELAY2( int( args[1] ))

def cmd_leds( args ):
    if len(args) != 9:
        send("!leds: needs 9 ints 0-65535")
        return
    for led, arg in zip( LEDS, args ):
        pwm.set( int( arg ) )

def cmd_fans( args ):
    if len(args) != 4:
        send("!fans: needs 4 ints 0-65535")
        return
    for pwm, arg in zip( FAN_PWM, args ):
        pwm.duty_u16( int( arg ) )

def cmd_beep( args ):
    if len(args) != 1:
        send("!beep: needs 1 int 100-20000")
        return
    freq = int( args[0] )
    if freq:
        BUZZER_PWM.freq( freq )
        BUZZER_PWM.duty_u16( 32768 )
    else:
        BUZZER_PWM.duty_u16( 0 )

def cmd_run( args ):
    send("#already in main.py\n")
    cmd_stat()

def cmd_stat( args=None ):
    global TACH_COUNTERS
    tc = TACH_COUNTERS
    TACH_COUNTERS = [ 0,0,0,0 ]
    send( ">tach %s\n" % tc )   # send comment line
    send( ">button %s\n" % PIN_BUTTON() )   # send comment line

def cmd_led( args ):
    LEDS[int(args[0])].set(int(args[1]))

def cmd_led_mode( args ):
    global LED_MODE
    LED_MODE = args[0]

COMMANDS = {
    "relays"    : cmd_relays,
    "leds"      : cmd_leds,
    "fans"      : cmd_fans,
    "beep"      : cmd_beep,
    "run"       : cmd_run,
    "stat"      : cmd_stat,
    "led"       : cmd_led,
    "led_mode"  : cmd_led_mode,
}

def parse( line ):
    # print( line )
    if len(line) < 1:
        send("!no command\n")
    cmd = line[0]
    args = line[1:]
    func = COMMANDS.get( cmd )
    if func:
        func( args )
    else:
        send("!unknown command %s\n" % cmd)

def command_line():
    cmd_stat()
    while True:
        line = getline()
        try:
            print( line )
            if not line:
                continue
            if parse( line.strip().split() ):
                break
        except Exception as e:
            print(e)
            send( "!%r\n" % e )

if __name__ == "__main__":
    command_line()