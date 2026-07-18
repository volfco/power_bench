import asyncio
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discover_atorch
import discover_atorch_spp
import meter_ble


class DiscoverAtorchTests(unittest.TestCase):
    def test_discovery_filters_deduplicates_and_sorts_atorch_devices(self):
        devices = [
            SimpleNamespace(name="Other", address="AA:00"),
            SimpleNamespace(name="UD18-1", address="AA:02"),
            SimpleNamespace(name="at24", address="AA:01"),
            SimpleNamespace(name="UD18-new", address="AA:02"),
        ]
        with patch.object(
            meter_ble.BleakScanner,
            "discover",
            new=AsyncMock(return_value=devices),
        ):
            found = asyncio.run(meter_ble.discover_atorch_devices(2.5))

        self.assertEqual(
            [(device.name, device.address) for device in found],
            [("at24", "AA:01"), ("UD18-new", "AA:02")],
        )

    @patch(
        "discover_atorch.discover_atorch_devices",
        new_callable=AsyncMock,
    )
    def test_cli_lists_name_and_mac_address(self, discover):
        discover.return_value = [
            SimpleNamespace(name="UD18", address="AA:BB:CC:DD:EE:FF")
        ]
        stdout = StringIO()

        with redirect_stdout(stdout):
            return_code = discover_atorch.main(["--timeout", "1.5"])

        self.assertEqual(return_code, 0)
        discover.assert_awaited_once_with(1.5)
        self.assertEqual(
            stdout.getvalue(),
            "NAME\tMAC ADDRESS\nUD18\tAA:BB:CC:DD:EE:FF\n",
        )

    @patch(
        "discover_atorch.discover_atorch_devices",
        new_callable=AsyncMock,
        return_value=[],
    )
    def test_cli_reports_when_no_atorch_device_is_found(self, _discover):
        stderr = StringIO()

        with redirect_stderr(stderr):
            return_code = discover_atorch.main([])

        self.assertEqual(return_code, 1)
        self.assertEqual(stderr.getvalue(), "No Atorch devices found.\n")


class DiscoverAtorchSppTests(unittest.TestCase):
    @patch("discover_atorch_spp.subprocess.run")
    def test_discovery_resolves_names_from_bluez_cache(self, run):
        run.side_effect = [
            SimpleNamespace(
                returncode=0,
                stdout=(
                    "[NEW] Device 46:AF:4E:55:56:06 AT24_SPP\n"
                    "[CHG] Device 00:00:00:02:9A:CB RSSI: -68\n"
                    "[CHG] Device AA:BB:CC:DD:EE:FF RSSI: -72\n"
                ),
                stderr="",
            ),
            SimpleNamespace(
                returncode=0,
                stdout=(
                    "Device 00:00:00:02:9A:CB S1BWT_SPP\n"
                    "Device 46:AF:4E:55:56:06 AT24_SPP\n"
                    "Device AA:BB:CC:DD:EE:FF Headphones\n"
                    "Device 11:22:33:44:55:66 DL24_SPP\n"
                ),
                stderr="",
            ),
        ]

        found = discover_atorch_spp.discover_atorch_spp_devices(1.2)

        self.assertEqual(
            [(device.name, device.address) for device in found],
            [
                ("AT24_SPP", "46:AF:4E:55:56:06"),
                ("S1BWT_SPP", "00:00:00:02:9A:CB"),
            ],
        )
        self.assertEqual(
            run.call_args_list[0].args[0],
            ["bluetoothctl", "--timeout", "2", "scan", "bredr"],
        )
        self.assertEqual(run.call_args_list[1].args[0], ["bluetoothctl", "devices"])

    @patch("discover_atorch_spp.discover_atorch_spp_devices")
    def test_cli_lists_name_and_mac_address(self, discover):
        discover.return_value = [
            discover_atorch_spp.ClassicDevice(
                name="S1BWT_SPP", address="00:00:00:02:9A:CB"
            )
        ]
        stdout = StringIO()

        with redirect_stdout(stdout):
            return_code = discover_atorch_spp.main(["--timeout", "1.5"])

        self.assertEqual(return_code, 0)
        discover.assert_called_once_with(1.5)
        self.assertEqual(
            stdout.getvalue(),
            "NAME\tMAC ADDRESS\nS1BWT_SPP\t00:00:00:02:9A:CB\n",
        )

    @patch("discover_atorch_spp.discover_atorch_spp_devices", return_value=[])
    def test_cli_reports_when_no_spp_device_is_found(self, _discover):
        stderr = StringIO()

        with redirect_stderr(stderr):
            return_code = discover_atorch_spp.main([])

        self.assertEqual(return_code, 1)
        self.assertEqual(stderr.getvalue(), "No Atorch SPP devices found.\n")


if __name__ == "__main__":
    unittest.main()
