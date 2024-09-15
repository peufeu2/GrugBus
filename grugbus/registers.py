#!/usr/bin/python
# -*- coding: utf-8 -*-

import struct, datetime, math
from pymodbus.pdu import ExceptionResponse

class RegBase( ):
    """
        ** Base class for registers.

        This class models a register in a modbus server, which can either be remote (modbus slave)
        or local, if we're running a local server.

        What this class does:
        - automatically convert between local data types and on-the-wire data types.
        - fixpoint scale factor conversion
        - handle byte and word order
        - multi-word registers (Uint32 = 2 modbus registers)
        - array registers (several consecutive registers of same type forming an array)

        ** Example:

        Slave (server) has signed 32 bit integer register (two words) that contains Voltage, 
        with a value of 1 in the register meaning 0.01 Volts. 
        To model this:

        What matters for choosing a register class is how data is encoded on the server/transmission side.
        In this example, data on the server is signed 32 bit integer, therefore register class is RegS32. 
        Data will be converted into float volts locally.

        So we instantiate RegS32() with tnstance parameters:
        nvalues        = 1 since this register contains one signed 32-bit integer value
        key            = "output_voltage" ; it will be inserted into parent device object and accessible as device.output_voltage
        unit_value      0.01, because a value of 1 in the register means 0.01 Volts.
        unit            string "Volts" or "V"

        Then, when reading:
        Slave sends a 32-bit integer value 1234

        Result after read():
        self.raw_value = 1234       integer value that what sent on the wire
        self.value = 12.34          after conversion

        self.value is simply raw_value*unit_value

        Due to python type conversion rules, if unit_value is a float, self.value will also be a float, which is 
        the right thing here. If the register contains flags or other things that require the exact value to be
        written, make sure to set unit_value=1 so both value and raw_value are the same.

        Likewise, write function expects values (not raw_value).

        See DeviceBase() for bulk read/writes.

        See code below for rest of documentation.
    """
    def __init__( self, 
        fcodes,
        addr        : int,
        nvalues     : int,
        key          : str,
        unit_value,
        unit        : str,              
        user_type   : str,
        decimals    : int,
        name        : str,
        description : str,
        little_endian : bool = False,
        swap_words    : bool = False
        ):
        """
Args:
    fcodes:         Function codes supported by register: int or tuple of ints, for example 4 for an input reg, or (3,6,16) for a holding reg
    addr:           Actual register address to use on the bus,  this is not the useless "register number" that requires "offset" (sometimes)
    nvalues:        Number of values for array registers. Useful for  "serial number" encoded as several consecutive U16 registers.
                    Note nvalues=1 for 32-bit registers, because it is one 32-bit value, not two 16-bit values.
    key         :   Machine-readable name, like "power_exported_today", must be unique
    unit_value  :   Scale factor to convert raw values to unit values.
                    Example: data transmitted on the bus is 123, if unit_value=0.01, then self.value is 1.23
    unit        :   example "kWh"
    user_type   :   type for self.value, can be "bool", "int", "float", "bitfield". Mostly to avoid converting ints encoding bitfields to float.
    decimals    :   number of decimals or None, used by format_value() to export pretty numbers to MQTT
    name        :   Human readable name, like "Power Exported Today""
    description :   Unhelpful hints from Chinese manual the data was taken from
    little_endian:  Byte order on the bus. Little-endian when True. Applies for 16 and 32 bit values. 
    swap_words  :   Can use this to swap words and bytes. Per register, in case an evil device has different byte ordering per register.
        """

        # Lowest function code, corresponding to read, must be first
        if isinstance(fcodes, int): self.fcodes  = (fcodes,)
        else:                       self.fcodes  = tuple(sorted(fcodes))

        # self.device is the device this register is physically located in, it will be registered by device.add_register()
        self.device     = None 

        # Init member varibles
        self.addr          = addr        
        self.nvalues       = nvalues      
        self.key           = key          
        self.unit_value    = unit_value or 1
        self.unit          = unit        
        self.name          = name        
        self.description   = description
        self.little_endian = little_endian
        self.swap_words    = swap_words

        # user_type is used to check that no unit conversions happen on stuff that shouldn't have any
        # (for example converting bitfields to float) and can be used by upper layers, for example
        # it is not useful to calculate the average of a value that doesn't represent anything
        # physical, like a flag or a bitfield
        if user_type not in ("bool","int","float","bitfield"):
            raise ValueError( "user_type <%s> not supported for %s" % (user_type,key))
        self.user_type = user_type

        if self.user_type == "float":
            self.unit_value = float(self.unit_value)    # cast unit_value to float to make sure self.value will always be a float
            if decimals == None:
                self.decimals = -round(math.log10(abs(self.unit_value)))
            else:
                self.decimals = int(decimals)
            format_value_fstr = "%%.0%df" % max( self.decimals, 1 )
            # adding 0.0 converts -0.0 into 0.0
            self._format_value = lambda v: format_value_fstr % (0.0 + round( v, self.decimals ))
            self.format_value = lambda: format_value_fstr % (0.0 + round( self.value, self.decimals ))

        else:
            if decimals not in (0,None):
                raise ValueError( "%s: user_type <%s> requires decimals=0 or None" % (self.key,user_type,))
            self.decimals = None
            if self.unit_value not in (1,-1):
                raise ValueError( "%s: user_type <%s> requires unit_value=1 or -1 (integer)" % (self.key,user_type,))
            self.unit_value = int(self.unit_value)     # cast unit_value to int  to make sure self.value will always be an int
            self._format_value = lambda v: "%d"%v
            self.format_value = lambda: "%d"%self.value

        # derived classes initialization
        self._init2()

        #   quick member functions to avoid if's everywhere.
        if self.nvalues == 1:   #   This register contains one single value
            if not hasattr( self, "_post_decode"   ): self._post_decode   = self._post_decode_single
            if not hasattr( self, "_pre_encode"    ): self._pre_encode    = self._pre_encode_single
            if not hasattr( self, "_set_raw_value" ): self._set_raw_value = self._set_raw_value_single
        else:                   #   It contains an array, so use appropriate functions
            if not hasattr( self, "_post_decode"   ): self._post_decode   = self._post_decode_array
            if not hasattr( self, "_pre_encode"    ): self._pre_encode    = self._pre_encode_array
            if not hasattr( self, "_set_raw_value" ): self._set_raw_value = self._set_raw_value_array

        # These will be updated on read()
        self.value      = None  # value after scale and unit conversion
        self.raw_value  = None  # raw value as seen on bus

    # Must be overriden
    def _init2( self ):
        raise NotImplementedError

    def set_device( self, device ):
        self.device = device

    # Virtual/Struct registers will override this to yield the actual registers they are wrapping
    def get_physical_regs( self ):
        yield self

    ########################################################
    #   Remote server access functions
    #   Not used when *WE* are the server
    ########################################################

    async def read( self ):
        """
        Reads this register from the remote server. 
        This requires this object to be linked to the device it is physically in via DeviceBase::__init__()
        """
        # Use the range read operation to read this register, to avoid code duplication
        await self.device.read_regs( (self,) )
        return self.value

    async def write( self, value=None ):
        """
        Writes this register to the remote server. 
        This requires this object to be linked to the device it is physically in via DeviceBase::__init__()
        """
        if value != None:
            self.value = value
        await self.device.write_regs( (self,) )

    async def write_if_changed( self, value ):
        """
        Writes this register to the remote server. 
        This requires this object to be linked to the device it is physically in via DeviceBase::__init__()
        """
        if value == self.value:
            return
        self.value = value
        await self.device.write_regs( (self,) )
        return True

    ########################################################
    #   Local server functions
    #   These handle setting the local register value
    #   so it is ready to be requested by a remote client.
    #   Not used when we access a remote server.
    ########################################################

    def set_value( self, v ):
        """
        Sets register value and converts it to raw_value using unit scale factor.
        _set_raw_value() is called during encoding before value is sent on the wire
        so it is usually not necessary to call this function.
        """
        self.value = v
        self._set_raw_value()

    ########################################################
    #   Data conversion functions
    #   are used in both cases (remote and local server)
    #  
    #   decode()    modbus wire format -> local types
    #   encode()    local types -> modbus wire format
    #
    ########################################################

    def decode( self, fcode, data ):
        """
        Decodes modbus wire data into local data type.
        Called by device.read_regs()
        Must be overriden
        """
        raise NotImplementedError

    def _post_decode_single( self, x ):
        """called at the end of decode() to set units and data type"""
        self.raw_value = x = x[0]
        self.value = v = self.unit_value*x  # unit_value is int or float depending on user_type, to return proper type to user
        return v

    def _post_decode_array( self, x ):
        """called at the end of decode() to set units and data type"""
        self.raw_value = x
        self.value = v = [ self.unit_value*v for v in x ]  # unit_value is int or float depending on user_type, to return proper type to user
        return v

    # convert value to raw_value
    def _set_raw_value_single( self ):
        self.raw_value = round( self.value / self.unit_value )

    def _set_raw_value_array( self ):
        self.raw_value = [ round( v / self.unit_value ) for v in self.value ]

    # called before encode, returns data to transmit
    # all it does is make sure raw_value is updated and wrap it in []

    def _pre_encode_single( self ):
        self._set_raw_value_single()
        return [ self.raw_value ]

    def _pre_encode_array( self ):
        self._set_raw_value_array()
        return self.raw_value

########################################################
#   Bools
########################################################

class RegBool( RegBase ):
    """
        Boolean register, could be a coil or a discrete input
    """
    def _init2( self ):
        # for bool registers, one word is still one register (one bit)
        self.word_length  = self.nvalues
        if self.user_type != "bool":
            raise ValueError( "user_type <%s> not supported for RegBool %s" % (user_type,key))

    def decode( self, fcode, data ):
        """
        Args:
            fcode:     function code used during this read operation, passed again as verification, because fcodes are a great source of bugs
            data:      words corresponding to this register, must contain self.word_length uint16
        """
        assert fcode in self.fcodes
        if len(data) != self.word_length:
            raise IndexError( "decode(): data passed contains %d words, but we expect %d words to decode %s" % (len(data),self.word_length,self.key))
        return self._post_decode( data ) 

    def encode( self ):
        """
            Returns words to pass to modbus.write_register or write_registers
        """
        return [ bool(v) for v in self._pre_encode() ]

   # convert value to raw_value
    def _set_raw_value_single( self ):
        self.raw_value = bool( self.value )

    def _set_raw_value_array( self ):
        self.raw_value = [ bool( v ) for v in self.value ]

########################################################
#   Ints
########################################################

class Reg16( RegBase ):
    """
        Base class for word-based (16 bit and 32 bit) registers (holding and input regs)
    """

    def _init2( self ):
        # make struct codes for conversion between wire data and python data
        # see decode() for how it's used
        self._struct_decode_unpack = ("<" if self.little_endian else ">") + self.nvalues*self._struct_decode_unpack
        self.byte_length  = struct.calcsize( self._struct_decode_unpack )
        self.word_length  = self.byte_length //2
        self._struct_decode_pack = ">" + self.word_length*"H"

    def decode( self, fcode, data ):
        """
        Args:
            fcode:     function code used during this read operation, passed again as verification, because fcodes are a great source of bugs
            data:      words corresponding to this register, must contain self.word_length uint16
        """
        assert fcode in self.fcodes
        # if len(data) != self.word_length:
            # raise IndexError( "decode(): data passed contains %d words, but we expect %d words to decode %s" % (len(data),self.word_length,self.key))
        
        # modbus returns uint16 words not bytes, that won't work with signed registers, so we convert
        # back into bytes and use struct to convert again into the correct type.
        # we could use modbus' type conversion but it doesn't support
        # different signed/unsigned types on reading multiple registers.
        # struct will perform type, range and length verification for us, so this is useful
        # even if both struct codes are the same and it does no conversion, hence the
        # commented test above, which is useless
        data = struct.unpack( self._struct_decode_unpack, struct.pack( self._struct_decode_pack, *data ))
        return self._post_decode( data )    # apply units

    def encode( self ):
        """
            Returns words to pass to modbus.write_register or write_registers
        """
        # struct will perform type and range verification for us, so this is useful
        # even if both struct codes are the same and it does no conversion
        return struct.unpack( self._struct_decode_pack, struct.pack( self._struct_decode_unpack, *self._pre_encode() ))
        
class Reg32( Reg16 ):
    def decode( self, fcode, data ):
        """
        Args:
            fcode:     function code used during this read operation, passed again as verification, because fcodes are a great source of bugs
            data:      words corresponding to this register, must contain self.word_length uint16
        """
        assert fcode in self.fcodes
        # if len(data) != self.word_length:
            # raise IndexError( "decode(): data passed contains %d words, but we expect %d words to decode %s" % (len(data),self.word_length,self.key))

        if self.swap_words:
            for n in range( 0, len(data), 2 ):
                data[n],data[n+1] = data[n+1],data[n]

        # convert uint16 words from modbus into the requested data type, also checks length of data
        data = struct.unpack( self._struct_decode_unpack, struct.pack( self._struct_decode_pack, *data ))
        return self._post_decode( data )

    def encode( self ):
        """
            Returns words to pass to modbus.write_register or write_registers
        """
        # struct will perform type and range verification for us, besides conversion
        # if an exception happens here, you probably wrote a float into an integer register and tried to write it
        data = struct.unpack( self._struct_decode_pack, struct.pack( self._struct_decode_unpack, *self._pre_encode() ))
        if self.swap_words:
            for n in range( 0, len(data), 2 ):
                data[n],data[n+1] = data[n+1],data[n]
        return data

#
#   Only difference between signed and unsigned is the struct code
#
class RegU16( Reg16 ):
    _struct_decode_unpack = "H"

class RegS16( Reg16 ):
    _struct_decode_unpack = "h"

class RegU32( Reg32 ):
    _struct_decode_unpack = "L"

class RegS32( Reg32 ):
    _struct_decode_unpack = "l"

########################################################
#   Floats
########################################################

class RegFloat( Reg32 ):
    """
        RegFloat does not mean the local value is a float.
        It means the value transmitted on the wire is a float.

        If the register is fixed point, you need an integer register
        with a unit scale factor instead.
    """
    _struct_decode_unpack = "f"

    def _init2( self ):
        super()._init2()
        if self.user_type != "float":
            raise ValueError( "user_type <%s> not supported for RegFloat %s" % (self.user_type,self.key))

   # convert value to raw_value
    def _set_raw_value_single( self ):
        self.raw_value = self.value/self.unit_value

    def _set_raw_value_array( self ):
        self.raw_value = [ v/self.unit_value for v in self.value ]

    # Convert NaN to None
    def nonan( self, v ):
        return self.unit_value*v if math.isfinite(v) else None

    def _post_decode_single( self, x ):
        """called at the end of decode() to set units and data type"""
        self.raw_value = x = x[0]
        self.value = v = self.nonan(x)  # unit_value is int or float depending on user_type, to return proper type to user
        return v

    def _post_decode_array( self, x ):
        """called at the end of decode() to set units and data type"""
        self.raw_value = x
        self.value = v = [ self.nonan(v) for v in x ]  # unit_value is int or float depending on user_type, to return proper type to user
        return v

########################################################
#   Bitfield Registers (not finished)
########################################################

class BitfieldMixin():
    def init_bits_n( self, bits ):
        self.bits = { bit_name:(bit,bool(active_high)) for bit,active_high,bit_name in bits }

    def get_bits( self ):
        self.bits = r = {}
        value = self.value
        for bit_name,(bit_bit,active_high) in self.bits.items():
            bit = bool(value & (1<<bit_bit))
            bit_name[k] = bit == active_high
        return r

    def get_on_bits( self ):
        r = set()
        value = self.value
        if value == None:
            return ()
        for bit_name,(bit_bit,active_high) in self.bits.items():
            if value & (1<<bit_bit):
                if active_high:
                    r.add( bit_name )
            elif not active_high:
                r.add( bit_name )
        return r

    def get_bit_value( self, bit_name ):
        bit_bit,active_high = self.bits[bit_name]
        return bool( self.value & (1<<bit_bit) )

    def bit_is_active( self, bit_name ):
        bit_bit,active_high = self.bits[bit_name]
        return bool( self.value & (1<<bit_bit) ) == active_high


########################################################
#   Struct registers (not finished)
########################################################

class RegStruct( ):
    """
    Struct Register
    These are made of several registers, just like a C struct
    Not derived from RegBase, as they do not exist in the device, do not have address, etc.
    """
    def __init__( self, 
        key         : str,
        name        : str,
        registers     : list
        ):

        self.fcodes    = registers[0].fcodes
        self.key       = key
        self.name      = name
        self.unit      = None
        self.unit_value= None
        self.registers = registers
        self.value     = None
        self.device    = None

        # compute address range
        self.addr        = min( reg.addr for reg in self.registers )
        self.word_length = max( reg.addr+reg.word_length for reg in self.registers ) - self.addr

        for reg in registers:
            if hasattr( self, reg.key ):
                raise KeyError( "Register key %s conflicts with member variable name of RegStruct %s" % (reg.key, self.key) )
            setattr( self, reg.key, reg )

    def set_device( self, device ):
        self.device = device
        for reg in self.registers:
            reg.set_device( device )

    # Get actual registers in the struct
    def get_physical_regs( self ):
        for reg in self.registers:
            yield from reg.get_physical_regs()

    # decode a data block for the entire struct
    # we don't let the device handle the registers one by one because we want
    # to read it in one single chunk.
    def decode( self, fcode, data ):
        for reg in self.registers:
            offset = reg.addr - self.addr
            reg.decode( fcode, data[ offset:(offset+reg.word_length) ] )
        return self._post_decode()

    # encode struct into a data block
    # we don't let the device handle the registers one by one because we want
    # to write it in one single chunk.
    def encode( self ):
        self._pre_encode()
        data = [None] * self.word_length
        for reg in self.registers:
            offset = reg.addr - self.addr
            data[ offset:(offset+reg.word_length) ] = reg.encode()
        return data

    async def read( self ):
        await self.device.read_regs( (self,) )

    async def write( self ):
        return await self.device.write_regs( (self,) )

class RegDateTimeSplit( RegStruct ):
    """
        DateTime stored into 6 registers (Year Month Day Hour Minute Second)
        With Y2K bug (Year is 2 digit)
    """
    def __init__( self,
        key         : str,
        name        : str,
        registers   : list
        ):
        assert len(registers) == 6      # 6 regs for YMDhms
        super().__init__( key, name, registers )

    def _post_decode( self ):
        YMDhms = [ r.value for r in self.registers ]
        if YMDhms[0] < 100:
            YMDhms[0] += 2000
        dt = datetime.datetime( *YMDhms )
        self.value = dt.isoformat()
        self.dt    = dt

    # def _pre_encode( self ):

#
#   This is not a register in the device, but is useful to store something and
#   treat it like a register

class FakeRegister:
    def __init__( self, key, value, user_type, decimals ):
        self.key = key
        self.value = value
        if user_type == "float":
            format_value_fstr = "%%.0%df" % max( decimals, 1 )
            # adding 0.0 converts -0.0 into 0.0
            self._format_value = lambda v: format_value_fstr % (0.0 + round( v, decimals ))
            self.format_value = lambda: format_value_fstr % (0.0 + round( self.value, decimals ))
        else:
            self._format_value = lambda v: "%d"%v
            self.format_value = lambda: "%d"%self.value



