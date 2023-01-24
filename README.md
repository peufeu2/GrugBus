# GrugBus controls Solis inverters via Modbus

"Dude, Modbus is outdated! When there's a blackout, I'd much rather use a JSON cloud API to control my backup inverter!"

This repository contains:

1) The Grugbus library, convenient abstraction layer powered by pymodbus.
2) An application using it to control Solis hybrid solar inverters via modbus.

# What's GrugBus?

    reg_list = (
              self.battery_voltage,                    
              self.battery_current,                    
              self.battery_current_direction,          
              self.bms_battery_soc,                    
              self.bms_battery_health_soh,             
              self.bms_battery_voltage,                
              self.bms_battery_current,                
              self.bms_battery_charge_current_limit,   
              self.bms_battery_discharge_current_limit,
            )
    await self.read_regs( self.reg_list )

    print( self.battery_voltage.value )   
            
GrugBus puts lipstick on Modbus and brings it up to modern Neanderthal tech level, including:

- No need to remember registers' number, address, offset, function code, etc.

- Automatic handling of types, units, fixed point, floats, bitfields, etc.

Example #1: you define an Uint16 register that contains Volts, with a unit value of 0.1V.
If the register contains 2305, this means 230.5V. This is the last time you'll see this,
reg.value is a float, and it's in volts. Also works for writes.

- Client and server support. In client mode, the register API can read/write to a server.
In server mode, it can read/write to a pymodbus datastore to prepare data to be served.
This is used in modbus_mitm.py to man-in-the-middle the link between an inverter and its
smartmeter.

- Bulk register reads up to 40x faster than single reads.

Most modbus commands don't take much longer to read 10 registers instead of just one.
In the above example, grugbus will split the list of registers into chunks that fit into
the maximum transaction size, and read the registers you asked for in the minimum number 
of transactions. 

read_regs() and write_regs() accept a list of registers with any combination of data types, 
sizes, modbus function codes, with continguous addresses or not.

This also works for writes. Bulk writes are split into contiguous ranges (ie without holes)
to avoid overwriting innocent bystander registers that are not specified in the list. 

- Multiple client support

The pymodbus instance is protected by a mutex, so several Devices can use the same port.

- Automated register list generation from datasheet PDF tables copypasted into spreadsheet
(see helper script in devices/)

Most of the documentation is the code.

# Controlling Solis inverters via Modbus

I have one (soon two) Solis S5-EH1P-6K inverters. The readable register list is available online, but to get to the interesting writable registers that allow full control of the inverter, a NDA must be signed in blood.

This uses an alternate approach that should allow full control of any inverter featuring a modbus smartmeter port: man-in-the-middle attack.

How it works: The code uses a RS485 interface to request data from the main smartmeter. Then, using another RS485 interface, it fakes a smartmeter and the inverter queries it. This allows manipulation of meter data seen by the inverter. The main item is active_power, which the inverter's internal feedback loop attempts to bring to zero.

If we add a constant P to active_power, the inverter will think the house is consuming P more watts, so it will try to export this power.

If we substract a constant P to active_power, the inverter will think the house is exporting energy due to another inverter producing, so it will try to absorb this power. It will reduce its power export and, if P is high enough, switch to charging batteries.

Combined with the maximum battery charge/discharge registers and modbus control of "time of use" logic, this should allow complete control of battery charge/discharge and export/import power.

After some quick tests, this works, but the full code isn't written yet. Mostly because the main reason to do this is to coordinate export power on two Solis inverters, and I haven't installed the second one yet.

Besides that, it logs everything to MQTT, and stores data in a Clickhouse database, ready for Grafana.

Coming soon: power routing to resistive loads with MQTT driven dimmer.

Shopping list:

# Requirements

- Exceedcon EC04681-2014-BF connector for Solis inverter COM port, available on ebay. Pin1 = +5V, Pin2 = GND, Pin3 = RS485+, Pin4 = RS485-. Double check the pinout online before soldering.
- Several [USB-RS485 interfaces](https://www.waveshare.com/catalog/product/view/id/3629/s/usb-to-rs232-485-ttl/category/37/usb-to-rs232-485-ttl.htm?sku=22547)
- Orange Pi Lite or other similar device
- I used python 3.11, it probably works with earlier versions
- pip install pyserial (not serial! that's a different module)
- [pymodbus 3.1.1](https://github.com/pymodbus-dev/pymodbus) ; could also work with 3.1.0 but you'll need to alter the code to make sure the modbus server starts. There was a small [bug](https://github.com/pymodbus-dev/pymodbus/pull/1282) in 3.1.1 so make sure you get the fixed version.


# Logging MQTT data to clickhouse

A script is included, it's a bit raw but it does the job.

Clickhouse's major selling points for logging MQTT data are :

1) Ludicrous speed.

2) Well suited for time series data: asof joins, automatic aggregating materialized views, etc.

3) Compression. 

Clickhouse is way overkill for this application. It likes using a lot of RAM, so it's probably not the right choice for a tiny Pi. I'm running it on a PC that doubles as a NAS.

Data usage is about 1.2 bytes per row (topic, timestamp, float), so it's fine to log everything.

It is a column store database that stores data in an ordered manner. For this application the ordering key is (mqtt_topic, timestamp). MQTT topics are stored using LowCardinality(String) which puts strings into an automatic dictionary and only stores the key. Because rows are ordered on disk, all rows in the same page have the same topic, so they compress down to nothing. Timestamps are monotonously increasing, so they compress very well using Deltas. Float MQTT data item values also use a delta encoding and de-duplication. The largest contributor to data bloat is useless decimals in floating point values. 





