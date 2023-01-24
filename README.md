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
innocent bystanders.

- Multiple client support

The pymodbus instance is protected by a mutex, so several Devices can use the same port.

- Automated register list generation from datasheet PDF tables copypasted into spreadsheet
(see helper script in devices/)

Most of the documentation is the code.




