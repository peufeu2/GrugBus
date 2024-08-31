#!/usr/bin/python
# -*- coding: utf-8 -*-

import time, asyncio, datetime, logging, collections, traceback

# Device wrappers and misc local libraries
import grugbus
from grugbus.devices import Eastron_SDM630
import config
from misc import *

log = logging.getLogger(__name__)


########################################################################################
#
#       Main house smartmeter, grid side, meters total power for solar+home
#
#       This class reads the meter and publishes it on MQTT
#
#       Meter is read very often, so it gets its own serial port
#
########################################################################################
class SDM630( grugbus.SlaveDevice ):
    def __init__( self, modbus, modbus_addr, key, name, mqtt, mqtt_topic, mgr ):
        super().__init__( modbus, modbus_addr, key, name, Eastron_SDM630.MakeRegisters() ),
        self.mqtt        = mqtt
        self.mqtt_topic  = mqtt_topic
        self.mgr = mgr

        self.total_power_tweaked = 0.0
        self.event_power = asyncio.Event()  # Fires every time frequent_regs below are read
        self.event_all   = asyncio.Event()  # Fires when all registers are read, for slower processes

    async def read_coroutine( self ):
        # For power routing to work we need to read total_power frequently. So we don't read 
        # ALL registers every time. Instead, gather the unimportant ones in little groups
        # and frequently read THE important register (total_power) + one group.
        # Unimportant registers will be updated less often, who cares.
        reg_sets = ((
            self.total_power                      ,    # required for fakemeter
            self.total_volt_amps                  ,    # required for fakemeter
            self.total_var                        ,    # required for fakemeter
            self.total_power_factor               ,    # required for fakemeter
            self.total_phase_angle                ,    # required for fakemeter
            self.frequency                        ,    # required for fakemeter
            self.total_import_kwh                 ,    # required for fakemeter
            self.total_export_kwh                 ,    # required for fakemeter
            self.total_import_kvarh               ,    # required for fakemeter
            self.total_export_kvarh               ,    # required for fakemeter
        ),(
            self.total_power                      ,    # required for fakemeter
            self.phase_1_line_to_neutral_volts    ,    # required for fakemeter
            self.phase_2_line_to_neutral_volts    ,
            self.phase_3_line_to_neutral_volts    ,
            self.phase_1_current                  ,    # required for fakemeter
            self.phase_2_current                  ,
            self.phase_3_current                  ,
            self.phase_1_power                    ,
            self.phase_2_power                    ,
            self.phase_3_power                    ,
        ),(
            self.total_power                      ,    # required for fakemeter
            self.total_kwh                        ,    # required for fakemeter
            self.total_kvarh                      ,    # required for fakemeter
        ),(
            self.total_power                      ,    # required for fakemeter
            self.average_line_to_neutral_volts_thd,
            self.average_line_current_thd         ,
        ))

        # publish these to MQTT
        regs_to_publish = set((
            self.phase_1_line_to_neutral_volts    ,
            self.phase_2_line_to_neutral_volts    ,
            self.phase_3_line_to_neutral_volts    ,
            self.phase_1_current                  ,
            self.phase_2_current                  ,
            self.phase_3_current                  ,
            self.phase_1_power                    ,
            self.phase_2_power                    ,
            self.phase_3_power                    ,
            self.total_power                      ,
            self.total_import_kwh                 ,
            self.total_export_kwh                 ,
            self.total_volt_amps                  ,
            self.total_var                        ,
            self.total_power_factor               ,
            self.total_phase_angle                ,
            self.average_line_to_neutral_volts_thd,
            self.average_line_current_thd         ,
                ))

        tick = Metronome(config.POLL_PERIOD_METER)  # fires a tick on every period to read periodically, see misc.py
        last_poll_time = None
        try:
            while True:
                for reg_set in reg_sets:
                    try:
                        await self.connect()
                        await tick.wait()
                        data_request_timestamp = time.monotonic()    # measure lag between modbus request and data delivered to fake smartmeter
                        try:
                            regs = await self.read_regs( reg_set )
                        except asyncio.exceptions.TimeoutError:
                            # Nothing special to do: in case of error, read_regs() above already set self.is_online to False
                            pub = {}
                        else:
                            # offset measured power a little bit to ensure a small value of export
                            # even if the battery is not fully charged
                            self.total_power_tweaked = self.total_power.value
                            bp  = self.mgr.solis1.battery_power.value or 0       # Battery charging power. Positive if charging, negative if discharging.
                            soc = self.mgr.solis1.bms_battery_soc.value or 0     # battery soc, 0-100
                            if bp > 200:        self.total_power_tweaked += soc*bp*0.0001

                            #   Fill fakemeter fields
                            fm = self.mgr.solis1.fake_meter
                            fm.voltage                .value = self.phase_1_line_to_neutral_volts .value
                            fm.current                .value = self.phase_1_current               .value
                            fm.apparent_power         .value = self.total_volt_amps               .value
                            fm.reactive_power         .value = self.total_var                     .value
                            fm.power_factor           .value =(self.total_power_factor            .value % 1.0)
                            fm.frequency              .value = self.frequency                     .value
                            fm.import_active_energy   .value = self.total_import_kwh              .value
                            fm.export_active_energy   .value = self.total_export_kwh              .value
                            fm.active_power           .value = self.total_power_tweaked
                            fm.data_timestamp = data_request_timestamp
                            fm.is_online = self.is_online
                            fm.write_regs_to_context()

                            pub = { reg.key: reg.format_value() for reg in regs_to_publish.intersection(regs) }      # publish what we just read

                        pub[ "is_online" ]    = int( self.is_online )   # set by read_regs(), True if it succeeded, False otherwise
                        pub[ "req_time" ]     = round( self.last_transaction_duration,2 )   # log modbus request time, round it for better compression
                        if last_poll_time:
                            pub["req_period"] = data_request_timestamp - last_poll_time
                        last_poll_time = data_request_timestamp
                        self.mqtt.publish( self.mqtt_topic, pub )
                        await self.router.route()
                    except (KeyboardInterrupt, asyncio.exceptions.CancelledError):
                        return
                    except:
                        log.exception(self.key+":")
                        # s = traceback.format_exc()
                        # log.error(s)
                        # self.mqtt.mqtt.publish( "pv/exception", s )
                        await asyncio.sleep(0.5)
        finally:
            await self.router.stop()