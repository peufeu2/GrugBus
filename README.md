# GrugBus
Convenient MODBUS abstraction on top of pymodbus to control Solis inverters

"Dude, Modbus is outdated! I'd much rather use a Chinese JSON cloud API that gives five minutes old data and only works if my internet is up!"

This repository contains:

1) The Grugbus library
2) An application using it to control Solis hybrid solar inverter(s).

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
            
GrugBus brings Modbus up to modern Neanderthal tech level, including:

- No need to remember registers number, address, offset, function code, etc.

- Automatic handling of types, units, fixed point, floats, bitfields, etc.

Example: you define an Uint16 register that contains Volts, with a unit value of 0.1V.
If the register contains 2305, this means 230.5V. This is the last time you'll see this,
reg.value is a float, and it's in volts. Also works for writes.

- Client and server support. In client mode, the register API can read/write to a server.
In server mode, it can read/write to a pymodbus datastore to prepare data to be served.
This is used in modbus_mitm.py to man-in-the-middle the link between an inverter and its
smartmeter.

- Bulk register reads up to 40x faster than single reads.

Most modbus commands don't take much longer to read 10 registers instead of just one.
In the above example, grugbus will look at the list of registers to read, split it into
chunks that fit into the maximum transaction size, and read the registers you asked for
in the minimum number of transactions. This also works for writes, but in this case it will
split the registers to write into contiguous ranges (ie without holes) to avoid overwriting
innocent bystanders. It is of course possible to pass a list of registers with any
combination of data types.

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

- Exceedcon EC04681-2014-BF connector for COM port, available on ebay. Pin1 = +5V, Pin2 = GND, Pin3 = RS485+, Pin4 = RS485-
- Several [USB-RS485 interfaces](https://www.waveshare.com/catalog/product/view/id/3629/s/usb-to-rs232-485-ttl/category/37/usb-to-rs232-485-ttl.htm?sku=22547)
- Orange Pi Lite or other similar device




