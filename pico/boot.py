import machine, time, sys, os

# Inputs
PIN_BUTTON    = machine.Pin( 2 , machine.Pin.IN , machine.Pin.PULL_UP )
PIN_FAN_TACH1 = machine.Pin( 9 , machine.Pin.IN , machine.Pin.PULL_UP )
PIN_FAN_TACH2 = machine.Pin( 7 , machine.Pin.IN , machine.Pin.PULL_UP )
PIN_FAN_TACH3 = machine.Pin( 5 , machine.Pin.IN , machine.Pin.PULL_UP )
PIN_FAN_TACH4 = machine.Pin( 3 , machine.Pin.IN , machine.Pin.PULL_UP )

# Outputs
PIN_BUZZER    = machine.Pin( 11, machine.Pin.OUT, value=0 )
PIN_FAN_PWM1  = machine.Pin( 10, machine.Pin.OUT, value=1 ) # start fans OFF
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

LEDS = [ PIN_LED1, PIN_LED2, PIN_LED3, PIN_LED4, PIN_LED5, PIN_LED6, PIN_LED7, PIN_LED8, PIN_LED9 ]
LED_PWM = [ machine.PWM(pin, freq=25000) for pin in LEDS ]

BUZZER_PWM = machine.PWM( PIN_BUZZER, freq=440, duty_u16=0 )

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
    timeout   = 100,            # timeout in ms
    )

def send( s ):
    print( "send %r" % s.encode() )
    UART0.write( s.encode() )

def getline():
    s = UART0.readline()
    if s == None:
        return s
    else:
        return s.decode()

snake_pos = 0
def snake( beep=True ):
    global snake_pos
    if beep:
        BUZZER_PWM.freq( 440 )
        BUZZER_PWM.duty_u16( 0 if snake_pos else 32768 )
    snake_pos = (snake_pos+1) % 9
    for n, pwm in enumerate( LED_PWM ):
        pwm.duty_u16( max( 0, min( 65535, 10000*((n-snake_pos)%9) - 30000)))

def put( fname ):
    tmpname = fname + ".tmp"
    max_chunk_length = 1024
    chunk = 0
    with open( tmpname, "wb" ) as outfile:
        while True:
            snake( False )
            BUZZER_PWM.freq( 1320 )
            BUZZER_PWM.duty_u16( [32768,0][chunk & 1] )
            chunk += 1
            send( "%d\n" % max_chunk_length )
            chunk_length = int(getline().strip())
            outfile.write( UART0.read( chunk_length ))
            if chunk_length != max_chunk_length:
                break
    send( "#Done\n" )
    BUZZER_PWM.duty_u16( 0 )
    os.rename( tmpname, fname )

def parse( line ):
    # print( line )
    if len(line) < 1:
        send("!no command\n")
    elif line[0] == "run":
        send("#bye\n")
        return 1
    elif line[0] == "put":
        if len(line) < 2:
            send("!usage: put filename\n")
            return
        send("#put\n")
        put( line[1] )
    else:
        send("!unknown command\n")

def bootloader():
    while True:
        snake()
        send( "bootloader>\n" )   # send comment line
        line = getline()
        try:
            print( line )
            if not line:
                continue
            if parse( line.strip().split() ):
                # check syntax in the next module
                send("#test import main\n")
                import main
                send("#import main successful\n")
                break
        except Exception as e:
            print(e)
            send( "!%r\n" % e )

bootloader()