"""Atorch BLE power meter protocol parser.

Based on:
- https://github.com/CursedHardware/atorch-console/blob/master/docs/protocol-design.md
- https://github.com/lanrat/usb-meter
"""

import struct
from dataclasses import dataclass
from enum import IntEnum

MAGIC_HEADER = b"\xff\x55"
REPORT_PACKET_LEN = 36


class MessageType(IntEnum):
    REPORT = 0x01
    REPLY = 0x02
    COMMAND = 0x11


class DeviceType(IntEnum):
    AC = 0x01
    DC = 0x02
    USB = 0x03


@dataclass
class MeterReading:
    timestamp: float
    device_type: DeviceType
    voltage: float
    current: float
    power: float
    capacity: float
    energy: float
    temperature: float
    duration_hours: int
    duration_minutes: int
    duration_seconds: int
    usb_d_minus: float | None = None
    usb_d_plus: float | None = None
    frequency: float | None = None
    power_factor: float | None = None
    price: float | None = None
    watt: float | None = None


def _uint24_be(data: bytes, offset: int) -> int:
    return (data[offset] << 16) | (data[offset + 1] << 8) | data[offset + 2]


def _uint32_be(data: bytes, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def _uint16_be(data: bytes, offset: int) -> int:
    return struct.unpack_from(">H", data, offset)[0]


def verify_checksum(packet: bytes) -> bool:
    """Accept the legacy Atorch checksum and the additive S1BWT SPP variant."""
    if len(packet) < 4:
        return False
    payload = packet[2:-1]
    legacy_checksum = (sum(payload) & 0xFF) ^ 0x44
    spp_checksum = (sum(payload) + 0x57) & 0xFF
    return packet[-1] in (legacy_checksum, spp_checksum)


def build_command(device_type: DeviceType, command: int, value: int = 0) -> bytes:
    """Build a command packet to send to the meter."""
    packet = bytearray(10)
    packet[0] = 0xFF
    packet[1] = 0x55
    packet[2] = MessageType.COMMAND
    packet[3] = device_type
    packet[4] = command
    struct.pack_into(">I", packet, 5, value)
    payload = packet[2:-1]
    packet[-1] = (sum(payload) & 0xFF) ^ 0x44
    return bytes(packet)


def _parse_ac(data: bytes) -> MeterReading:
    return MeterReading(
        timestamp=0,
        device_type=DeviceType.AC,
        voltage=_uint24_be(data, 4) / 10,
        current=_uint24_be(data, 7) / 1000,
        power=0,
        capacity=0,
        # Energy counter unit is 0.01 kWh per tick (= 10 Wh) per the protocol doc —
        # stored here as Wh. The 10 Wh resolution is why per-run energy is integrated
        # from power samples instead (see plan.md); verify this scaling against a
        # known load on the first live run.
        energy=_uint32_be(data, 13) * 10,
        temperature=_uint16_be(data, 24),
        duration_hours=_uint16_be(data, 26),
        duration_minutes=data[28],
        duration_seconds=data[29],
        watt=_uint24_be(data, 10) / 10,
        price=_uint24_be(data, 17) / 100,
        frequency=_uint16_be(data, 20) / 10,
        power_factor=_uint16_be(data, 22) / 1000,
    )


def _parse_dc(data: bytes) -> MeterReading:
    return MeterReading(
        timestamp=0,
        device_type=DeviceType.DC,
        voltage=_uint24_be(data, 4) / 10,
        current=_uint24_be(data, 7) / 1000,
        power=0,
        capacity=_uint24_be(data, 10) * 10,
        energy=0,
        temperature=_uint16_be(data, 24),
        duration_hours=_uint16_be(data, 26),
        duration_minutes=data[28],
        duration_seconds=data[29],
        price=_uint24_be(data, 17) / 100,
    )


def _parse_usb(data: bytes) -> MeterReading:
    return MeterReading(
        timestamp=0,
        device_type=DeviceType.USB,
        voltage=_uint24_be(data, 4) / 100,
        current=_uint24_be(data, 7) / 100,
        power=0,
        capacity=_uint24_be(data, 10),
        energy=_uint32_be(data, 13) / 100,
        temperature=_uint16_be(data, 21),
        duration_hours=_uint16_be(data, 23),
        duration_minutes=data[25],
        duration_seconds=data[26],
        usb_d_minus=_uint16_be(data, 17) / 100,
        usb_d_plus=_uint16_be(data, 19) / 100,
    )


def parse_report(data: bytes, timestamp: float) -> MeterReading:
    """Parse a 36-byte report packet into a MeterReading."""
    if len(data) != REPORT_PACKET_LEN:
        raise ValueError(f"Expected {REPORT_PACKET_LEN} bytes, got {len(data)}")
    if data[0:2] != MAGIC_HEADER:
        raise ValueError(f"Invalid magic header: {data[0:2].hex()}")
    if data[2] != MessageType.REPORT:
        raise ValueError(f"Not a report packet (type=0x{data[2]:02x})")

    device_type = DeviceType(data[3])

    parsers = {
        DeviceType.AC: _parse_ac,
        DeviceType.DC: _parse_dc,
        DeviceType.USB: _parse_usb,
    }
    parser = parsers.get(device_type)
    if parser is None:
        raise ValueError(f"Unknown device type: 0x{data[3]:02x}")

    reading = parser(data)
    reading.timestamp = timestamp
    # AC meters report true (real) power directly in `watt`. Computing voltage*current
    # would give apparent power (VA), which overstates real watts whenever the power
    # factor < 1 (every switching PSU). DC/USB have no power factor, so V*I is correct.
    if reading.watt is not None:
        reading.power = reading.watt
    else:
        reading.power = round(reading.voltage * reading.current, 4)
    return reading


RESET_ALL = build_command(DeviceType.USB, 0x05)
RESET_ENERGY = build_command(DeviceType.USB, 0x01)
RESET_CAPACITY = build_command(DeviceType.USB, 0x02)
RESET_RUNTIME = build_command(DeviceType.USB, 0x03)
