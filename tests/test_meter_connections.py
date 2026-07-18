import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from discover_atorch_spp import ClassicDevice
from atorch_protocol import verify_checksum
import meter_ble
from meter_spp import SppMeterConnection


class FakeSocket:
    def __init__(self, chunks=(), connect_error=None):
        self.chunks = list(chunks)
        self.connect_error = connect_error
        self.connected_to = None
        self.closed = False

    def settimeout(self, timeout):
        self.timeout = timeout

    def connect(self, address):
        self.connected_to = address
        if self.connect_error:
            raise self.connect_error

    def recv(self, _size):
        return self.chunks.pop(0)

    def sendall(self, data):
        self.sent = data

    def close(self):
        self.closed = True


class SppMeterConnectionTests(unittest.TestCase):
    def test_read_packet_reassembles_fragmented_stream_and_skips_noise(self):
        packet = b"\xff\x55\x01" + bytes(range(33))
        conn = SppMeterConnection("00:00:00:02:9A:CB")
        conn._socket = FakeSocket([b"noise" + packet[:9], packet[9:]])

        result = asyncio.run(conn.read_packet(timeout=1.0))

        self.assertEqual(result, packet)

    @patch("meter_spp.socket.socket")
    def test_connect_tries_next_rfcomm_channel(self, socket_factory):
        first = FakeSocket(connect_error=OSError("refused"))
        second = FakeSocket()
        socket_factory.side_effect = [first, second]
        conn = SppMeterConnection("00:00:00:02:9A:CB", channels=(1, 2))

        result = conn._connect_blocking()

        self.assertIs(result, second)
        self.assertEqual(first.connected_to, ("00:00:00:02:9A:CB", 1))
        self.assertEqual(second.connected_to, ("00:00:00:02:9A:CB", 2))
        self.assertTrue(first.closed)


    def test_s1bwt_additive_checksum_is_accepted(self):
        packet = bytes.fromhex(
            "ff5501010004c40000720000500000000b00006402570244001e"
            "000d14333c000000009f"
        )

        self.assertTrue(verify_checksum(packet))
        self.assertFalse(verify_checksum(packet[:-1] + bytes([packet[-1] ^ 1])))


class MeterConnectionFallbackTests(unittest.TestCase):
    @patch("meter_ble.SppMeterConnection")
    @patch.object(
        meter_ble.BleakScanner,
        "find_device_by_address",
        new_callable=AsyncMock,
        return_value=None,
    )
    def test_explicit_mac_falls_back_to_spp_when_absent_from_ble(self, _find, spp):
        spp_client = MagicMock()
        spp_client.connect = AsyncMock(return_value="spp socket")
        spp.return_value = spp_client
        conn = meter_ble.MeterConnection("00:00:00:02:9A:CB", timeout=2.0)

        result = asyncio.run(conn.connect())

        self.assertEqual(result, "spp socket")
        spp.assert_called_once_with("00:00:00:02:9A:CB", timeout=2.0)


    @patch("meter_ble.BleakClient")
    @patch("meter_ble.SppMeterConnection")
    @patch.object(
        meter_ble.BleakScanner,
        "find_device_by_address",
        new_callable=AsyncMock,
    )
    def test_cached_spp_identity_is_not_connected_as_ble(
        self, find, spp, bleak_client
    ):
        find.return_value = SimpleNamespace(
            name="S1BWT_SPP",
            address="00:00:00:02:9A:CB",
        )
        spp_client = MagicMock()
        spp_client.connect = AsyncMock(return_value="spp socket")
        spp.return_value = spp_client

        result = asyncio.run(
            meter_ble.MeterConnection("00:00:00:02:9A:CB", timeout=2.0).connect()
        )

        self.assertEqual(result, "spp socket")
        bleak_client.assert_not_called()
        spp.assert_called_once_with("00:00:00:02:9A:CB", timeout=2.0)

    @patch("meter_ble.discover_atorch_spp_devices")
    @patch("meter_ble.BleakClient")
    @patch.object(
        meter_ble.BleakScanner,
        "find_device_by_filter",
        new_callable=AsyncMock,
    )
    def test_auto_discovery_prefers_ble(self, find_ble, bleak_client, discover_spp):
        device = SimpleNamespace(name="AT24", address="AA:BB:CC:DD:EE:FF")
        find_ble.return_value = device
        client = MagicMock()
        client.connect = AsyncMock()
        client.start_notify = AsyncMock()
        bleak_client.return_value = client

        result = asyncio.run(meter_ble.MeterConnection(timeout=2.0).connect())

        self.assertIs(result, client)
        discover_spp.assert_not_called()

    @patch("meter_ble.SppMeterConnection")
    @patch("meter_ble.discover_atorch_spp_devices")
    @patch.object(
        meter_ble.BleakScanner,
        "find_device_by_filter",
        new_callable=AsyncMock,
        return_value=None,
    )
    def test_auto_discovery_falls_back_to_spp(self, _find, discover_spp, spp):
        discover_spp.return_value = [
            ClassicDevice(name="S1BWT_SPP", address="00:00:00:02:9A:CB")
        ]
        spp_client = MagicMock()
        spp_client.connect = AsyncMock(return_value="spp socket")
        spp.return_value = spp_client

        result = asyncio.run(meter_ble.MeterConnection(timeout=2.0).connect())

        self.assertEqual(result, "spp socket")
        discover_spp.assert_called_once_with(2.0)
        spp.assert_called_once_with("00:00:00:02:9A:CB", timeout=2.0)


if __name__ == "__main__":
    unittest.main()
