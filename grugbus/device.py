#!/usr/bin/python
# -*- coding: utf-8 -*-

import logging, functools, asyncio, time
from pymodbus.pdu import ExceptionResponse
from pymodbus.exceptions import ModbusException, ConnectionException
from asyncio.exceptions import TimeoutError
import config
from misc import *

log = logging.getLogger(__name__)

class DeviceBase( ):
    """
        GrugBus brings modbus up to modern Neanderthal tech level, including:

            -   No need remember number, address, offset, function code, etc, just read()
            -   Bulk register reads up to 40x faster than single reads
                ...because most modbus commands take the same (very long) time no matter
                if we read one register or 40.
            -   Automated register list generation from datasheet PDF tables copypasted into spreadsheet
                (see helper script in devices/)

        This is the base class for both local server and remote server (slave),
        it represents "something with registers".

        The main point of this class is to contain a set of Registers that automatically 
        convert between local data types and on-the-wire data types.
    """
    def __init__( 
            self, 
            modbus, 
            bus_address,
            key,
            name,
            registers,
            max_regs_in_command=40,
            max_bits_in_command=200
            ):
        """
            :param  modbus:     Instance of pymodbus server or client
            :param  key:        Machine readable key like "garage_inverter"
            :param  name:       Human readable name like "Garage Roof Solar Inverter"
            :param  registers:  List of RegBase derived registers
            :param  max_regs_in_command:    Maximum number of word registers to read/write in one modbus command
            :param  max_bits_in_command:    Maximum number of bit registers to read/write in one modbus command
        """
        # pymodbus has its own mutex now
        # if not hasattr( modbus, "_async_mutex" ):   # mutex protects serial port if we have more than 1 device banging on it
            # modbus._async_mutex = asyncio.Lock()
        config.PYMODBUS_CLIENT_TWEAKS( modbus )
        self.modbus      = modbus
        self.bus_address = bus_address
        self.name        = name
        self.key         = key
        self.max_regs_in_command = {}
        for fcode in 1,2,5,15:  self.max_regs_in_command[fcode] = max_bits_in_command
        for fcode in 3,4,16:  self.max_regs_in_command[fcode] = max_regs_in_command

        self.registers  = []
        self.regs_by_key = {}
        self.addr_belongs_to_reg = {}
        self.regs_by_addr = {}

        self.last_transaction_timestamp = 0
        self.last_transaction_period    = 0
        self.last_transaction_duration  = 0
        self.default_retries = config.GRUGBUS_RETRIES+1    # 1 means 1 try and no retry

        # SDM120 does not like "write register", it needs "write multiple registers" even if there is just one
        self.force_multiple_regiters = False
        self.ratelimit_error_count = 0

        #
        #   Will be set to True if we have successful communication with the device,
        #   then set back to False if communication fails.
        self.is_online = False

        if modbus:
            self.set_modbus( modbus )

        # register registers
        for reg in registers:
            self.add_register( reg )

    def rate_limit_error( self, old_is_online, increment_counter ):
        if old_is_online:   # it was online and is no longer online.
            self.ratelimit_error_count = 0
            return "[was online]"
        else:
            if increment_counter:
                self.ratelimit_error_count += 1
            if self.ratelimit_error_count < config.GRUGBUS_RATE_LIMIT_ERRORS:
                return "[%s]" % self.ratelimit_error_count
            if self.ratelimit_error_count == config.GRUGBUS_RATE_LIMIT_ERRORS:
                return "[further messages suppressed]"

    def add_register( self, reg ):
            # link this object to the register for self.read()
            assert reg.device is None   # don't reuse register instances between Device instances
            reg.set_device( self )

            # sort them neatly by key to find them later
            # print( "%5d %s" % (reg.addr, reg.key) )
            if reg.key in self.regs_by_key:    # key must be unique
                raise KeyError( "Register key %s is duplicated" % reg.key )
            self.registers.append( reg )
            self.regs_by_key[reg.key] = reg
            if hasattr( self, reg.key ):
                raise KeyError( "Register key %s conflicts with member variable name of %s" % (reg.key, self.key) )

            setattr( self, reg.key, reg )

            # sort them also by (fcode,address) to find them when doing multi-reads
            if hasattr( reg, "addr" ):  # only physical registers
                # for fcode in reg.fcodes:
                    # c = (fcode,reg.addr)
                    # assert c not in self.regs_by_fcode_addr     # must be unique
                    # self.regs_by_fcode_addr[c] = reg
                self.regs_by_addr[reg.addr] = reg
                fcode = reg.fcodes[0]
                for a in range(reg.word_length):
                    k = (fcode,reg.addr+a)
                    if k in self.addr_belongs_to_reg:
                        self.dump_regs(True)
                        raise KeyError( "Registers %s and %s overlap, wrong register definition?" % (reg.key, self.addr_belongs_to_reg[k].key) )
                    self.addr_belongs_to_reg[k] = reg

    # def get_regs_in_range( self, fcode, start_addr, end_addr ):
    #     """
    #     Yields: registers fully contained in the address range.
    #     Note end_addr is not inclusive.
    #     If a multi-word register is not fully contained inside the address range,
    #     it will not be considered.
    #     """
    #     while start_addr < end_addr:
    #         reg = self.regs_by_fcode_addr.get( (fcode, start_addr) )
    #         if reg:
    #             start_addr += reg.word_length
    #             if start_addr > end_addr:   # only add this register if we have the whole data for it
    #                 break
    #             yield reg
    #         else:
    #             start_addr += 1

class SlaveDevice( DeviceBase ):
    """######################

        A remote modbus server.
        Manages bulk read/writes.

    ########################################
    """

    def set_modbus( self, modbus ):
        # read functions by modbus fcode
        self.modbus = modbus
        self._read_funcs = {
                1:  modbus.read_coils,
                2:  modbus.read_discrete_inputs,
                3:  modbus.read_holding_registers,
                4:  modbus.read_input_registers
            }

    async def connect( self ):
        if not self.modbus.connected:
            # async with self.modbus._async_mutex:
            # do not connect twice at the same time
            async with self.modbus._lock:
                if not self.modbus.connected:
                    log.info( "%s: modbus connect", self.key )
                    await self.modbus.connect()

    # function cache requires reg_list to be a tuple
    def reg_list_to_chunks( self, reg_list, _max_hole_size ):
        if len(reg_list) == 1:  # fast path
            reg, = reg_list 
            return [ (reg.fcodes[0], [(reg.addr, reg.addr+reg.word_length, reg)]) ]
        return self._reg_list_to_chunks( tuple(reg_list), _max_hole_size )

    @functools.cache
    def _reg_list_to_chunks( self, reg_list, _max_hole_size ):
        """
            Converts a list of registers into a list of address ranges
            in the form (fcode, start_addr, end_addr) to use bulk commands
            Not a generator because result is cached
        """
        reg_list = set(reg_list)
        ops = {}            # make a list of all addresses to hit, contains (start,end) of each register
        for reg in reg_list:
            ops.setdefault( reg.fcodes[0], [] ).append( (reg.addr, reg.addr+reg.word_length, reg) )

        result = []
        for fcode, addrs in ops.items():
            max_chunk = self.max_regs_in_command[ fcode ]

            if _max_hole_size == None:
                max_hole_size = int( self.max_regs_in_command[fcode]//2 )
            else:
                max_hole_size = _max_hole_size

            addrs = sorted( addrs, key=lambda a:a[0] )
            start_addr = addrs[0][0]
            end_addr   = addrs[-1][1]
            if max_hole_size and end_addr <= start_addr + max_chunk:
                result.append(( fcode, addrs ))     # It's already chunk-sized
            else:
                start_idx = 0
                chunk_end_addr = start_addr + max_chunk
                for idx in range(len(addrs)-1):
                    reg_end_addr = addrs[idx][1]
                    if reg_end_addr > chunk_end_addr:           # would register at [idx] be too long to fit in current chunk?
                        # print("chunk")
                        result.append(( fcode, addrs[start_idx:idx] )) # back off and make a chunk with previous addresses
                        start_idx  = idx                        # (not including this register)
                        chunk_end_addr = addrs[start_idx][0] + max_chunk
                    if reg_end_addr+max_hole_size < addrs[idx+1][0]:  # do we have a hole and is it too large?
                        # print("hole")
                        result.append(( fcode, addrs[start_idx:(idx+1)] ))          # make a chunk with previous addresses plus current one
                        start_idx  = idx+1
                        chunk_end_addr = addrs[start_idx][0] + max_chunk
                remain = addrs[start_idx:]
                if remain:
                    result.append(( fcode, remain ))            # process last record


        if config.LOG_MODBUS_REGISTER_CHUNKS:
            r = []
            for fcode, chunk in result:
                r.append( "    Chunk [%d,%d[:" % (chunk[0][0],chunk[-1][1]) )
                for reg_start_addr, reg_end_addr, reg in chunk:
                    r.append( "        %6d %2d: %s" % (reg_start_addr, reg_end_addr-reg_start_addr, reg.key))
            log.info( "%s: register groups:%s", self.key, "\n".join(r) )

        return result

    def reg_list_interleave( self, frequent_regs, all_regs ):
        for fcode, chunk in self.reg_list_to_chunks( all_regs , None ):
            yield [ reg for reg_start_addr, reg_end_addr, reg in chunk] + frequent_regs

    async def read_regs( self, read_list, retries=None, max_hole_size=None ):
        """
            Reads multiple registers. It is much faster than reading registers individually and is
            the preferred way versus calling read() on each register.
            Modbus transactions will be chunked to stay under the limit max_regs_in_command.

            All registers read will have their .value set.
            Registers read "by accident", because they sit between requested registers in a bulk read,
            will *not* be updated.

            Args:
                read_list: list of RegBase instances
            
            All registers need not have the same function code, this will issue the appropriate commands.

            TODO: remove this
            This releases the Modbus mutex between each register chunk (ie, between each modbus command)
            so other tasks trying to access this modbus interface get a chance to run.
        """
        retries = retries or self.default_retries
        old_is_online = self.is_online
        try:
            start_time = time.monotonic()
            # modbus_time = 0
            # mutex_time = 0
            update_list = []
            for fcode, chunk in self.reg_list_to_chunks( read_list, max_hole_size ):
                # print( fcode, ":", " ".join( "%d-%d" % (c[0],c[1]) for c in chunk ))
                func = self._read_funcs.get( fcode )
                if not func: 
                    raise ValueError( "Function code %s not supported for read_regs()" % fcode )

                # modbus bulk read
                start_addr  = chunk[0][0]
                end_addr    = chunk[-1][1]
                for retry in range( retries ):
                    await self.connect()
                    try:
                        # mutex_time -= time.monotonic()
                        # async with self.modbus._async_mutex:    # share same serial port between several tasks
                        # st = time.monotonic()
                        # mutex_time += st
                        resp = await func( start_addr, end_addr-start_addr, self.bus_address )
                        # modbus_time += time.monotonic() - st
                        if isinstance( resp, ExceptionResponse ):
                            raise ModbusException( str( resp ) )
                        if fcode != 2:
                            reg_data = resp.registers
                        else:
                            reg_data = resp.bits
                        # if self.modbus._async_mutex._waiters:
                            # wait until serial is flushed before releasing lock, 
                            # do not use asyncio sleep, we're in a hurry to release it
                            # time.sleep(0.001)  
                        update_list.append( (fcode, chunk, start_addr, reg_data) )
                        break
                    except (TimeoutError,ModbusException,ConnectionException) as e:
                        is_err = retry == retries-1
                        msg = self.rate_limit_error(old_is_online, is_err)
                        if not is_err:
                            if msg:
                                log.info( "Modbus read error: %s will retry %d/%d (%s)", self.key, retry+1, retries, e )
                        else:
                            if msg:
                                log.error( "Modbus read error: %s after %d/%d tries (%s) %s", self.key, retry+1, retries, e, msg )
                            raise
                        await asyncio.sleep(config.GRUGBUS_RETRY_WAIT_S)  # let other tasks use this serial port

            # Decode values and assign to registers. Do this in a separate loop after reading,
            # to make sure all registers were processed. Otherwise, due to the await above,
            # a mix of old and new values could be present in the object during the read
            # and seen by other coroutines
            result = []
            if update_list:
                self.is_online = True
                for fcode, chunk, start_addr, reg_data in update_list:
                    for reg_start_addr, reg_end_addr, reg in chunk:
                        offset = reg.addr - start_addr
                        reg.decode( fcode, reg_data[ offset:(offset+reg.word_length) ] )
                        result.append( reg )
                if not old_is_online:
                    log.info( "Modbus: %s (%s) is online" % (self.key, self.name) )
            return result
        except Exception as e:
            self.is_online = False
            raise
        finally:
            self._set_timings( start_time )
            cfg = config.LOG_MODBUS_REQUEST_TIME.get( self.key )
            if cfg:
                slow = self.last_transaction_duration > cfg[1]
                if "r" in cfg[0] or slow:
                    self.publish_modbus_timings()
                if slow:
                    # log.info("%s: slow modbus read: [mutex %.03fs modbus %.03fs]/%.03fs retry %s", 
                    #     self.key, mutex_time, modbus_time, self.last_transaction_duration, retry ) # , [reg.key for reg in read_list])
                    log.info("%s: slow modbus read: %.03fs retry %s", 
                        self.key, self.last_transaction_duration, retry ) # , [reg.key for reg in read_list])

    def _set_timings( self, start_time ):
        t = time.monotonic()
        if self.last_transaction_timestamp:
            self.last_transaction_period    = t - self.last_transaction_timestamp
        self.last_transaction_timestamp = t
        self.last_transaction_duration  = t - start_time


    def publish_modbus_timings( self ):
        if self.last_transaction_duration:
            self.mqtt.publish_value( self.mqtt_topic+"req_time",   round( self.last_transaction_duration, 2 ))
        if config.LOG_MODBUS_REQUEST_PERIOD and self.last_transaction_period:
            self.mqtt.publish_value( self.mqtt_topic+"req_period", round( self.last_transaction_period, 2 ))


    async def write_regs( self, write_list, retries=None ):
        """
            Writes multiple registers. 
            Modbus transactions will be chunked to stay under the limit max_regs_in_command.

            Writing needs more care than reading, so holes in the range are not allowed:
            if the range of addresses in write_listis not contiguous, several write commands
            will be issued.

            Multi-word registers are always written in one single modbus transaction.

            Some devices want groups of specific registers to be written in one single transaction.
            This may not occur if these registers are passed as part of a long list and it is
            split due to the maximum number of registers per transaction allowed by the device.
            In this case, issue write_regs() on these registers alone.

            Also it is better to make writes explicit in calling code.

            If the range contains only one word, a single write command will be issued.
            If it contains multiple words (including one 32-bit value which is 2 words)
            then a multi write command will be issued.

            Args:
                write_list: list of RegBase instances
                retries   : if there is a modbus timeout, will retry up to the number specified
        """
        retries = retries or self.default_retries
        old_is_online = self.is_online
        try:
            start_time = time.monotonic()
            update_list = []
            for fcode, chunk in self.reg_list_to_chunks( write_list, 0 ):
                # check address span of this write operation and build data buffer
                start_addr = chunk[0][0]
                end_addr   = chunk[-1][1]
                reg_data   = [None] * (end_addr-start_addr)

                # encode data in buffer
                for reg_start_addr, reg_end_addr, reg in chunk:
                    offset = reg.addr - start_addr
                    reg_data[ offset:(offset+reg.word_length) ] = reg.encode()

                # in case a register returned too much data
                assert len(reg_data) == end_addr-start_addr          

                # check for holes (if reg_list_to_chunks malfunctioned)
                # or if there was a struct register with a hole in the middle
                if None in reg_data:
                    raise IndexError("write_regs() cannot write a chunk of registers with a hole in it, as that would overwrite an unknown register")
                update_list.append( (fcode, start_addr, reg_data) )


            # Perform modbus writes. Do this in a separate loop after preparing data to write,
            # otherwise, due to the await, a mix of old and new values could be written.
            for fcode, start_addr, reg_data in update_list:
                for retry in range( retries ):
                    await self.connect()
                    try:
                        # async with self.modbus._async_mutex:
                        if 1:
                            if fcode in (1,5):     # we're dealing with bools (force coil)
                                if len(reg_data) == 1:    
                                    fcode = 5   # force single coil
                                    # print( "write_coil", fcode, start_addr, reg_data )
                                    resp = await self.modbus.write_coil( start_addr, reg_data[0], self.bus_address )
                                else:                     
                                    fcode = 15  # force multiple coils
                                    # print( "write_coils", fcode, start_addr, reg_data )
                                    resp = await self.modbus.write_coils( start_addr, reg_data, self.bus_address )
                            elif fcode in (3,6,16):   # we're dealing with words (registers)
                                if len(reg_data) == 1 and not self.force_multiple_regiters:    
                                    fcode = 6   # force single register
                                    # print( "write_register", fcode, start_addr, reg_data )
                                    resp = await self.modbus.write_register( start_addr, reg_data[0], self.bus_address )
                                else:                     
                                    fcode = 16  # force multiple registers
                                    # print( "write_registers", fcode, start_addr, reg_data )
                                    resp = await self.modbus.write_registers( start_addr, reg_data, self.bus_address )
                            else:
                                raise ValueError( "wrong function code %r in write_regs()" % (fcode,) )
                        if isinstance( resp, ExceptionResponse ):
                            raise ModbusException( str( resp ))
                        self.is_online = True
                        break
                    except (TimeoutError,ModbusException,ConnectionException) as e:
                        is_err = retry == retries-1
                        msg = self.rate_limit_error(old_is_online, is_err)
                        if not is_err:
                            if msg:
                                log.info( "Modbus write error: %s will retry %d/%d (%s)", self.key, retry+1, retries, e )
                        else:
                            if msg:
                                log.error( "Modbus write error: %s after %d/%d tries (%s) %s", self.key, retry+1, retries, e, msg )
                            raise
                        await asyncio.sleep(config.GRUGBUS_RETRY_WAIT_S)  # let other tasks use this serial port

        except Exception as e:
            self.is_online = False
            raise
        finally:
            self._set_timings( start_time )
            cfg = config.LOG_MODBUS_REQUEST_TIME.get( self.key )
            if cfg:
                slow = self.last_transaction_duration > cfg[1]
                if "w" in cfg[0] or slow:
                    self.publish_modbus_timings()
                if slow:
                    log.info("%s: slow modbus write: %.03fs %s", self.key, self.last_transaction_duration, [reg.key for reg in write_list])

    # for debugging
    def dump_all_regs( self, all=False ):
        for reg in self.registers:
            if all or reg.value != None and not reg.key.startswith("reserved"):
                print( "%s %5s %40s %s %s" % (reg.fcodes[0], reg.addr, reg.key, reg.value, reg.unit or "") )

    def dump_regs( self, regs ):
        for reg in regs:
            print( "%s %5s %40s %8s %2s (%04x)" % (reg.fcodes[0], reg.addr, reg.key, reg.format_value(), reg.unit or "", reg.raw_value) )



class LocalServer( DeviceBase ):
    """######################

        A local modbus server.
        This one is very simple because pymodbus does everything.

        init function difference:
            modbus contains ModbusSlaveContext

    ########################################
    """

    def set_modbus( self, modbus ):
        self.modbus = modbus
    
    def write_regs_to_context( self, regs=None ):
        """
            After we modified the value attribute of some registers, this will encode them
            and store them in the ModbusSlaveContext, ready to be served to any client
            who's querying us.
        """
        if regs == None:
            regs = self.registers

        for reg in regs:
            if reg.value != None:
                try:
                    v = list(reg.encode())
                except:
                    print(reg.key, reg.value)
                    raise
                else:
                    # self.modbus.setValues( reg.fcodes[0], reg.addr, [0] )
                    self.modbus.setValues( reg.fcodes[0], reg.addr, v )

if __name__ == "__main__":
    # Test chunk generation code
    from registers import *

    class dummy_modbus():
        def read_coils(): pass
        def read_discrete_inputs(): pass
        def read_holding_registers(): pass
        def read_input_registers(): pass

    def makereg(clas,addr,length=1):
        return clas(  (3, 6, 16), addr,  length, "r%d"%addr, None, None, '' , '' )
    def makedev(regs):
        return SlaveDevice( dummy_modbus(), 1, "test", "test", regs, max_regs_in_command=10 )
    def makelist():
        return [makereg(RegU16,0,1), makereg(RegU16,1,4)] + [makereg(RegU32,5+a*2,1) for a in range(10)]
    def p( td, regs, max_hole_size ):
        chunkn = [None]*(regs[-1].addr+regs[-1].word_length)
        regn   = list(chunkn)
        for cn, (fcode, chunk) in enumerate(td.reg_list_to_chunks( regs, max_hole_size )):
            print( cn, ":", " ".join( "%d-%d" % (c[0],c[1]) for c in chunk ))
            for reg_start_addr, reg_end_addr, reg in chunk:
                for o in range(reg.word_length):
                    chunkn[reg.addr+o] = cn
        for reg in regs:
            for a in range( reg.addr, reg.addr+reg.word_length ):
                regn[a] = reg.key

        for c,r in zip(chunkn,regn):
            print( "%10s %10s" % (c,r) )


    td = makedev( makelist() )
    p( td, td.registers, None )
    p( td, td.registers, 0 )
    l = td.registers
    del_idx=2
    print( "del", l[del_idx].key )
    del l[del_idx]
    del l[6]
    p( td, l, 0 )











