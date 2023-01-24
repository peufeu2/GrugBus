#!/usr/bin/python
# -*- coding: utf-8 -*-

#
#   Register data from datasheet PDF
#   Extracted from Solis_S5_EH1P_6K_2020.csv
#

from grugbus import registers
from . import Solis_S5_EH1P_6K_2020

class U16Bitfield( registers.RegU16, registers.BitfieldMixin ):
    pass

def MakeRegisters():
    regs = list( Solis_S5_EH1P_6K_2020.MakeRegisters() )
    r = { reg.key: reg for reg in regs }

    def setup_bitfield( key, bits ):
        reg = r[key]
        reg.__class__ = U16Bitfield
        reg.init_bits_n( bits )

    setup_bitfield( "operating_status", (
            ( 0, True , "Normal Operation" ),
            ( 1, True , "Initializing" ),
            ( 2, True , "Controlled turning OFF" ),
            ( 3, True , "Fault leads to turning OFF" ),
            ( 4, True , "Stand-by" ),
            ( 5, True , "Limited Operation (temperature, frequency, etc.)" ),
            ( 6, True , "Limited Operation (external reason)" ),
            ( 7, True , "Backup overload" ),
            ( 8, False, "Load fault" ),
            ( 9, False, "Grid fault" ),
            (10, False, "Battery fault" ),
            (12, True , "Grid Surge warning" ),
            (13, True , "Fan fault warning" ),
        ))

    setup_bitfield( "fault_status_1_grid", (
            ( 0, True , "No grid" ),
            ( 1, True , "Grid over voltage" ),
            ( 2, True , "Grid under voltage" ),
            ( 3, True , "Grid over frequency" ),
            ( 4, True , "Grid under frequency" ),
            ( 5, True , "Unbalanced grid" ),
            ( 6, True , "Grid frequency fluctuation" ),
            ( 7, True , "Grid reverse current" ),
            ( 8, True , "Grid current tracking error" ),
            ( 9, True , "Meter COM Fail" ),
            (10, True , "FailSafe" ),
        ))

    setup_bitfield( "fault_status_2_backup", (
            ( 0, True , "Backup overvoltage fault" ),
            ( 1, True , "Backup overload fault" ),
        ))

    setup_bitfield( "fault_status_3_battery", (
            ( 0, True , "Battery not connected" ),
            ( 1, True , "Battery overvoltage Check" ),
            ( 2, True , "Battery undervoltage Check" ),
        ))

    setup_bitfield( "fault_status_4_inverter", (
            ( 0, True , "DC overvoltage" ),
            ( 1, True , "DC Bus overvoltage" ),
            ( 2, True , "DC Bus unbalanced voltage" ),
            ( 3, True , "DC Bus undervoltage" ),
            ( 4, True , "DC Bus unbalanced voltage 2" ),
            ( 5, True , "DC overcurrent on A circuit" ),
            ( 6, True , "DC overcurrent on B circuit" ),
            ( 7, True , "DC input interference" ),
            ( 8, True , "Grid overcurrent" ),
            ( 9, True , "IGBT overcurrent" ),
            (10, True , "Grid interference 02" ),
            (11, True , "AFCI self-check" ),
            (12, True , "Arc fault reserved" ),
            (13, True , "Grid current sampling fault" ),
            (14, True , "DSP self-check error" ),
        ))

    setup_bitfield( "fault_status_5_inverter", (
            (  0, True , "Grid interference" ),
            (  1, True , "Over dc components" ),
            (  2, True , "Over temperature protection" ),
            (  3, True , "Relay check protection" ),
            (  4, True , "Under temperature protection" ),
            (  5, True , "PV insulation fault" ),
            (  6, True , "12V undervoltage protection" ),
            (  7, True , "Leak current protection" ),
            (  8, True , "Leak current self check protection" ),
            (  9, True , "DSP initial protection" ),
            ( 10, True , "DSP B protection" ),
            ( 11, True , "Battery overvoltage hardware fault" ),
            ( 12, True , "LLC hardware overcurrent" ),
            ( 13, True , "Grid transient overcurrent" ),
            ( 14, True , "CAN COM FAIL" ),
            ( 15, True , "DSP COM FAIL" ),
        ))

    setup_bitfield( "bms_battery_fault_information_01", (
            ( 1, True , "Over voltage protection" ),
            ( 2, True , "Under voltage protection" ),
            ( 4, True , "Over temperature protection" ),
            ( 4, True , "Under temperature protection" ),
            ( 5, True , "Over temperature charging protection" ),
            ( 6, True , "Under temperature charging protection" ),
            ( 7, True , "Discharging overcurrent protection" ),
        ))

    setup_bitfield( "bms_battery_fault_information_02", (
            ( 0, True , "Charging overcurrent protection" ),
            ( 3, True , "BMS internal protection" ),
            ( 4, True , "Battery module unbalanced" ),
        ))

    setup_bitfield( "epm_flags", (
            ( 0, True , "EPM ON" ),
            ( 1, True , "FailSafe" ),
        ))

    setup_bitfield( "meter_placement", (
            ( 0, True , "Meter on house side" ),
            ( 1, True , "Meter on grid side" ),
            ( 2, True , "CT in grid" ),
            ( 3, True , "Parallel PV inverter CT detection switch" ),
            ( 4, True , "EPM ON" ),
            ( 5, True , "Failsafe Switch " ),
        ))

    setup_bitfield( "energy_storage_mode", (
            ( 0, True , "Self use mode" ),
            ( 1, True , "Time-charging optimized revenue mode" ),
            ( 2, True , "Off-grid mode" ),
            ( 3, True , "Battery wakeup enable" ),
            ( 4, True , "Reserve battery mode" ),
            ( 5, False, "Allow battery charge from grid" ),
        ))

    return regs

if __name__ == "__main__":
    MakeRegisters()

