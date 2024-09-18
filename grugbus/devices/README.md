# How to make a register map for a modbus device.

First we need a list of registers, for example a datasheet or technical manual, preferably in PDF form. Modbus registers should be in a table. This means, if we open it with a PDF reader that supports table copy-paste (like Foxit) it is possible to paste the whole table into a spreadsheet. Some PDF readers will turn tables into a mess of plaintext, they are not suitable for this.

Open Eastron_SDM120.csv in LibreOffice and look at it. Each line is a register. Everything is case-insensitive.

For a more complex example, you can check the Solis_S5_EH1P_6K.csv file, which is probably the most complete list of modbus registers for Solis S5 EH1P available online. It was compiled from various sources, plus a few exclusive reverse-engineered registers.

Columns:

1) function: modbus function code, should be 1,2,3 or 4. This gives the modbus register type (coil,input,holding,etc).
2) addr: modbus address as seen on the bus. If there is an offset, it should be applied to it. Grugbus will make no offset correction.
3) addr_end: normally empty. If the register is an array, addr_end is the address of the last word, inclusive. If the datasheet says "words 10-16: serial number registers" then addr=10 and addr_end=16.
3) type: the python register class to use, without "Reg" prefix: U16, S16, U32, S32, Float, etc.
4) unit: empty if there is no unit, otherwise the base unit of the register. If the datasheet says "Value 100 means 1V", then enter 0.01V. The tool will extract the float scale value and unit name.
5) name: human readable register name, something like "Battery Voltage"
6) name_for_id: register identifier, "battery_voltage". Leave empty to have the tool generate it. If the name contains characters that aren't proper for identifiers, you can use this column to override it.
7) usertype: "int", "float", "bool", or "bitfield". This is the type returned in register.value
8) decimals: How many decimals to use for pretty formatting. Does not influence the value conversion in any way.
9) description: it is stored but currently ignored in the code. However, if you do some reverse engineering, this is where to put notes.

Once datasheet information is copied and cleaned up, you can save the CSV.

Next open register_format_data.py, go to the end, replace the CSV filenames with your new one, and run the program.

It should output a lot of debug info, perhaps stop with errors or ask some questions, then if everything's okay, it will write a .py file with the same name as the original csv file, which contains a register factory (MakeRegisters) for initializing your device.

To see how this file is used, check modbus_mitm.py and search for MakeRegisters to see how to instanciate a modbus device.

Included google keywords:

# Eastron SDM630 Smartmeter modbus register map

Pros: Excellent three phase smartmeter, measures down to 0W, high resolution (1W), fast reaction time to load changes and quick settling (<0.1s), fast modbus (can be polled >10 times/s), perfect for zero export control or diverting loads. Can be used as three phase plus neutral, three phase without neutral, two phase, etc. Everything is configurable, including backlight. No bulky CTs.

Cons: Computation of import/export is different than French utility meter "Linky" so import/export measurement will be wrong in three phase mode. Linky computes the sum of power used on all phases, so one inverter on one phase can offset the consumption of the house on other phases. SDM630 computes per-phase, so it will count this as one phase exporting and the others importing.

# Eastron SDM120 Smartmeter modbus register map

Pros: High resolution (1W), fast reaction time to load changes and quick settling (<0.1s), fast modbus (can be polled >10 times/s).

Cons: Any power lower than 50W reads as zero, so not usable for accurate measurements of small loads or zero export control. Backlight cannot be turned off.

# Fronius TS 100A-1 Smartmeter modbus register map

Pros: None

Cons: Very slow measurement, updated every 2 seconds: unusable for zero export or diverting loads.

# Acrel ACR10RD16TE4 Smartmeter modbus register map

Not tested.

# EVSE ABB TERRA modbus register map

See EVSE_ABB_Terra.md file.

# Solis S5-EH1P-6K modbus register map



