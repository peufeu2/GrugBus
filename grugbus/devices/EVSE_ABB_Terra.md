# ABB Terra AC EVSE

Review summary: this wallbox is retarded.

# User interface

It consists of a few LEDs. No buttons or controls of any kind.

# Connectivity

It has WiFi and ethernet, which it can use for Modbus-TCP, talking to an OCPP server, and talking to ABB cloud servers.
I do not know if it will automatically update its firmware and brick itself if it has network access. This seems to require the app.
It talks to the app via bluetooth.
It has a RS485 Modbus-RTU port. I'll use this, because I already put a modbus cable to the parking space where the EVSE is installed.

# Use without modbus

It works fine and starts charging as soon as the car is plugged.

# Use with modbus

ABB TerraConfig is the "installer app" which must be used to configure the EVSE. It connects to the EVSE via bluetooth and requires an ABB cloud account. 
The app is very buggy. Repeat configuration until the settings seem to register, then disconnect, reconnect, read the settings back, and repeat again until the settings actually register. Maybe.
There are two modes for modbus:

1) Single EVSE as modbus master talking to a smartmeter (sold separately, only approved brands obviously). 
2) Multiple EVSE as modbus slaves with a central controller. This is the one I'm using. It doesn't matter that there's only one EVSE. There's a register to tell it how much current to use, which is all that's needed for PV load balancing.

After choosing this, it will enable modbus settings like address, baud, parity etc.

# ChargeSync app

ABB ChargeSync is the "user" app which must be used to enable charging. It connects to the EVSE via bluetooth and requires yet another ABB cloud account.
It is of course slow, buggy and cumbersome, requires login and password every time which it doesn't autofill, plus a cloud connection. 
Both apps will force update the EVSE firmware to the latest version before letting you use them. As the philosopher said, focus on the path and not the destination, and meditate while it  installs every version in between.

Obviously, the first item on the todolist is to get rid of the app.

# RFID

There is a RFID reader. It is possible to add RFID cards to authorize charging. It proudly beeps when a RFID card is scanned.
Obviously, it doesn't work to enable charging.
I guess the authorized RFID cards list is not stored in the EVSE memory but in ABB cloud servers. You can use your own OCPP server by setting its url in the app, so that may be an option. I didn't try.
This means without wifi or ethernet, RFID is useless. No further testing.

# The almighty hidden "Charging_Enabled" state variable.

Without RFID, only ChargeSync can enable charging.
Once charging is enabled, the EVSE will remember the setting even if it loses power. Thus the goal is to never reset this setting to false! Otherwise, it requires using the app again.
There is a "Start Stop Charging Session" modbus register:

- It is write only, so it cannot be read.
- Setting it to "Stop" will also reset Charging_Enabled, requiring use of the app to enable it back again, a sort of self destruct feature.

The solution seems to be to leave the register to its default of "start" (or set it once), then use the app to enable charging, and never touch it ever again.
The device remembers this, even across power down and reboot, so the app needs to be used once at installation, then never again.

Writing to register to set it to "start" seems to require unplugging and replugging the car's plug. Better leave it alone.

# Communication timeout

When set in modbus mode, the EVSE will expect to receive modbus messages once in a while, specifically the value in "Communication Timeout" register. If it does not receive enough attention, it will pout and stop working. Thus, "Communication timeout" should be set to something like 20 minutes or more, and some registers should be read periodically.

# Getting it to work

Using modbus, we can read:

- Current, voltage, power etc

- Error code, which honors the tradition of always reading zero no matter how much it fails

- Socket state (plugged in EVSE, locked in EVSE, plugged in car)

This is an OR combination of:
0x0001 Cable plugged in charging station.
0x0010 Cable locked in charging station.
0x0100 Cable plugged in electric vehicle.

To charge you want 0x111: plugged to station, locked in station, plugged in EV.
To pull out the cable you want 0x001 or 0x101 so it is not locked.

- Charging state (stuff like waiting for the car, connected, energized, charging)

0 – State A  Idle.
1 - State B1 EV Plug in, pending authorization.
2 - State B2 EV Plug in, EVSE ready for charging (PWM).
3 - State C1 EV Ready for charge, S2 closed (no PWM).
4 - State C2 Charging Contact closed, energy delivering.
5 - "Others"

We can set:

- Current limit
- Socket lock
- Start/stop charge, with a big red "do not touch" sticker on it.

# Locking and unlocking the socket

The station's automatic behavior is adequate:
- When it detects an EV at the other end of the cable, it locks automatically.
- When the EV is no longer detected, it unlocks automatically.

The "Socket Lock" modbus register can be read and written to, at different addresses. During a charging session, it reads:

| What's happening                                            | Socket State                                 |
| ---------------------------------                           | -------------------------------------------- |
| Idle                                                        | 0x000 no cable                               |
| User plugs cable in station                                 | 0x001 cable plugged in station               |      
| User plugs cable in EV                                      | 0x101 cable plugged in station and EV        |             
| Auto lock (takes 2 seconds)                                 | 0x111 cable plugged/locked in station and EV |                    
| User stops charge from EV                                   | 0x111 cable plugged/locked in station and EV |             
| User unplugs cable from EV                                  | 0x011 cable plugged/locked in station        |             
| Auto unlock (2 seconds)                                     | 0x001 cable plugged in station               |      
| User unplugs cable from station                             | 0x000 no cable                               |

Writing to the Socket Lock register:
- The socket will lock automatically when the EV is detected, no matter what the register says.
- Writing 1 to the Socket Lock register will override Auto Unlock and keep the plug locked until 0 is written.

Thus the Socket Lock register doesn't control the lock, it should be called Socket Force Lock instead.

Writing 0 in the Socket Lock register while it is charging seems to confuse the EVSE and tell it the charging session is over. It then enters state 5 and pouts until the cable is unplugged then plugged in again on the EV side, at this point it will consider charging.

Modbus writes to this register wait until the motor has moved the locking latch to respond, which takes more than one second. This means the bus is tied during this time, no other commands can be issued either to the EVSE or any other slave on the same bus.

The socket can be unlocked using the app... but only if the station is configured not to use modbus! When it uses modbus, the button in the app is inactive.

Thus the Socket Lock register also gets a "do not touch" sticker. The automatic behavior is adequate anyway.

# Starting and stopping charge

The "Start Stop Charging Session" register cannot be used, so the charging session is always left active.

The result is quite practical: 

- When the cable is plugged in the EV, charging starts and the station locks the socket.
- When charge is stopped from the EV, charging stops and the station unlocks the socket.

I have a Type 1 car: the plug on the EV side has a thumb latch which releases it from the socket and allows it to be pulled out. The car has a switch to detect if the thumb latch is pressed, which interrupts charging. Thus the natural motion of grabbing the plug, pressing the latch and pulling it out corresponds to the electrically safe sequence of cutting off current before unplugging. As soon as it is unplugged, the station detects it and cuts power to the cable.

With a type 2 car, there is a lock and no thumb latch, and the station has no button to interrupt charging either. So extracting the plug requires the user to stop the charge from the EV side, which will probably involve navigating some kind of retarded user interface. If you have a type 2 car I would recommend a station which has usable user interface instead, like a button.

# Controlling charge current

This is done via the modbus Current Limit register.

- Setting it below 6A simply pauses the charge, but does not reset Charging_Enabled, so it does not require using the app to restart it. 
- Setting it to 6A or more restarts the charge.

All this does is send a command via the PWM wire to the car's onboard charger, so for my Nissan e-nv200:

- Going from "paused" to "charging" takes 10-30 seconds.
- When charging, it takes about 2 seconds to react to a change in current limit.

