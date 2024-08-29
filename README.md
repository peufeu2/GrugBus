# What is GrugBus?

GrugBus is an abstraction layer built on top of pymodbus. It puts lipstick on Modbus, including:

- Automatic handling of types, units, fixed point, floats, bitfields, scaling, byte order, endianness, long values using several holding registers, etc.

Example: the device offers an Int32 register that contains Watts, with a unit value of 0.1W. If the register contains 4305, this means 430.5W. Grugbus will convert between the register value and the user value, in this case a float expressed in Watts.

- Modbus registers are accessed by name, no need to remember their address, offset, function code, etc.

- async/await support

- Client and server support. In client mode, the register API can read/write to a server.
In server mode, it can read/write to a pymodbus datastore to prepare data to be served.
This is used in modbus_mitm.py to man-in-the-middle the link between an inverter and its
smartmeter.

- Multiple client support: several Devices can share the same RS485 bus by sharing the same pymodbus instance (protected by mutex).

- Automated register list generation from datasheet PDF tables 

Use a pdf reader that supports tabular copypaste (like Foxit) and paste the modbus register table into a spreadsheet. Arrange it to the proper format (see helper script in devices/ for details). Save as CSV. Run the helper script, it will generate python code with all the registers definitions. This saves a lot of time.

- Bulk register reads up to 40x faster than single reads.

    self.reg_list = (
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
      
Most modbus commands don't take much longer to read 10 registers instead of just one.
In the above example, grugbus will split the list of registers into chunks that fit into
the maximum transaction size, and read the registers you asked for in the minimum number 
of transactions. 

read_regs() and write_regs() accept a list of registers with any combination of data types, 
sizes, modbus function codes, with continguous addresses or not.

Example: one Uint16 register at address 10 and one Int32 register at address 11-12 will be
read in one transaction.

This also works for writes. Bulk writes are split into contiguous ranges (ie without holes)
to avoid overwriting innocent bystander registers that are not specified in the list. 

# Contents of this repository

1) Grugbus library in its directory
2) All the other files in the root directory are parts of my photovoltaic management, they are provided as examples. The main one is modbus_mitm.py, which implements a man-in-the-middle between the inverter and the house smartmeter, which can allow tweaking the reported power value on the fly, Solis inverter control, MQTT, etc.

# Controlling Solis inverters via Modbus

I have one (soon two) Solis S5-EH1P-6K inverters. The readable register list is available online, but to get to the interesting writable registers that allow full control of the inverter, a NDA must be signed in blood.

This uses an alternate approach that should allow full control of any inverter featuring a modbus smartmeter port: man-in-the-middle.

How it works: The code uses a RS485 interface to request data from the main smartmeter. Then, using another RS485 interface, it fakes a smartmeter and the inverter queries it. This allows manipulation of meter data seen by the inverter. The main item is active_power, which the inverter's internal feedback loop attempts to bring to zero.

If we add a constant P to active_power, the inverter will think the house is consuming P more watts, so it will try to export this power.

If we substract a constant P to active_power, the inverter will think the house is exporting energy due to another inverter producing, so it will try to absorb this power. It will reduce its power export and, if P is high enough, switch to charging batteries.

Combined with the maximum battery charge/discharge registers and modbus control of "time of use" logic, this should allow complete control of battery charge/discharge and export/import power.

After some quick tests, this works, but the full code isn't written yet. Mostly because the main reason to do this is to coordinate export power on two Solis inverters, and I haven't installed the second one yet.

Besides that, it logs everything to MQTT, and stores data in a Clickhouse database, ready for plotting via Bokey.

There is also code to control ABB Terra EVSE according to PV excess.

Coming soon: power routing to resistive loads with MQTT driven dimmer.

Shopping list:

# Requirements

- Exceedcon EC04681-2014-BF connector for Solis inverter COM port, available on ebay. Pin1 = +5V, Pin2 = GND, Pin3 = RS485+, Pin4 = RS485-. Double check the pinout online before soldering.
- Several [USB-RS485 interfaces](https://www.waveshare.com/catalog/product/view/id/3629/s/usb-to-rs232-485-ttl/category/37/usb-to-rs232-485-ttl.htm?sku=22547) ; Waveshare also offers an [isolated two port](https://www.waveshare.com/product/iot-communication/wired-comm-converter/usb-to-rs232-uart-rs485/usb-to-2ch-rs485.htm) interface which is nice (two ports for the price of one) but the ports are not isolated between each other. It's a good match when hacking the Solis inverters, since each requires one RS485 port for the fake meter and one for control.
- Orange Pi Lite or other similar device
- I used python 3.11, it probably works with earlier versions
- pip install pyserial (not serial! that's a different module)
- [pymodbus 3.1.1](https://github.com/pymodbus-dev/pymodbus) ; could also work with 3.1.0 but you'll need to alter the code to make sure the modbus server starts. There was a small [bug](https://github.com/pymodbus-dev/pymodbus/pull/1282) in 3.1.1 so make sure you get the fixed version.


# Logging MQTT data to clickhouse

Script included, a bit raw but it does the job. I didn't want to run the database on the Pi because it is too slow and the SD card is tiny. So I run the database on a desktop PC. That isn't always on, so I needed a way to store data on the Pi when the database PC is not available.

mqtt_buffer.py: This runs on the Raspberry Pi and stores all mqtt traffic in compressed form. It also acts as a server, able to serve past data and stream current data.

mqtt_clickhouse.py: This runs on the PC. On launch it will connect to the above server to retrieve past data and catch up with the current state. It inserts everything into clickhouse tables.

bokehplot.py: a [bokeh app to plot the data](https://www.youtube.com/watch?v=rZxsFkaUimE&lc=Ugz4bzv51YXSnqZ2K194AaABAg). Currently WIP.

Clickhouse's major selling points for logging MQTT data are :

1) Ludicrous speed.

2) Well suited for time series data: asof joins, automatic aggregating materialized views, etc.

3) Compression. 

Clickhouse is way overkill for this application. It likes using a lot of RAM, so it's probably not the right choice for a tiny Pi. I'm running it on a PC that doubles as a NAS.

Data usage is about 1.2 bytes per row (topic, timestamp, float), so it's fine to log everything.

It is a column store database that stores data in an ordered manner. For this application the ordering key is (mqtt_topic, timestamp). MQTT topics are stored using LowCardinality(String) which puts strings into an automatic dictionary and only stores the key. Because rows are ordered on disk, all rows in the same page have the same topic, so they compress down to nothing. Timestamps are monotonously increasing, so they compress very well using Deltas. Float MQTT data item values also use a delta encoding and de-duplication. The largest contributor to data bloat is useless decimals in floating point values. 





