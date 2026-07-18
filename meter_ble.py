"""BLE-first connection handler with Classic SPP fallback for Atorch meters."""

import asyncio
import logging
import time

from bleak import BleakClient, BleakScanner

from discover_atorch_spp import discover_atorch_spp_devices
from meter_spp import SppMeterConnection

logger = logging.getLogger(__name__)

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
CHARACTERISTIC_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
DEVICE_NAME_PREFIXES = ("UD18", "AT24", "J7", "DL24")

def is_atorch_device(device) -> bool:
    """Return whether a BLE device name matches a supported Atorch family."""
    print(device.name, device)
    return bool(
        device.name
        and any(device.name.upper().startswith(prefix) for prefix in DEVICE_NAME_PREFIXES)
    )


async def discover_atorch_devices(timeout: float = 10.0):
    """Scan for and return supported Atorch devices, deduplicated by address."""
    devices = await BleakScanner.discover(timeout=timeout)
    matches = {device.address: device for device in devices if is_atorch_device(device)}
    return sorted(
        matches.values(),
        key=lambda device: ((device.name or "").casefold(), device.address),
    )

class MeterConnection:
    """Prefer BLE and fall back to Classic Bluetooth SPP when BLE is unavailable."""

    def __init__(self, mac_address: str | None = None, timeout: float = 10.0):
        self.mac_address = mac_address
        self.timeout = timeout
        self._client: BleakClient | None = None
        self._spp: SppMeterConnection | None = None
        self._notify_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)

    async def _notification_handler(self, sender, data: bytearray):
        self._notify_queue.put_nowait(bytes(data))

    async def _find_ble_device(self):
        if self.mac_address:
            logger.info("Looking for BLE meter %s", self.mac_address)
            return await BleakScanner.find_device_by_address(
                self.mac_address,
                timeout=self.timeout,
            )

        logger.info("Scanning for Atorch BLE meter...")
        return await BleakScanner.find_device_by_filter(
            lambda discovered, _: is_atorch_device(discovered),
            timeout=self.timeout,
        )

    async def connect(self):
        device = await self._find_ble_device()
        if device is not None:
            logger.info("Found BLE meter %s (%s)", device.name, device.address)
            self._client = BleakClient(device, timeout=self.timeout)
            await self._client.connect()
            await self._client.start_notify(
                CHARACTERISTIC_UUID,
                self._notification_handler,
            )
            logger.info("Connected over BLE and notifications started")
            return self._client

        if self.mac_address:
            spp_address = self.mac_address
            logger.info(
                "%s was not found over BLE; trying Classic Bluetooth SPP",
                spp_address,
            )
        else:
            logger.info("No Atorch BLE meter found; scanning for SPP meters")
            devices = await asyncio.to_thread(
                discover_atorch_spp_devices,
                self.timeout,
            )
            if not devices:
                raise RuntimeError("No Atorch BLE or SPP meter found during scan")
            spp_address = devices[0].address
            logger.info("Found SPP meter %s (%s)", devices[0].name, spp_address)

        self._spp = SppMeterConnection(spp_address, timeout=self.timeout)
        return await self._spp.connect()

    async def read_packet(self, timeout: float | None = None) -> bytes:
        if self._spp is not None:
            return await self._spp.read_packet(timeout=timeout)
        try:
            return await asyncio.wait_for(self._notify_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError("No packet received within timeout")

    async def send(self, data: bytes):
        if self._spp is not None:
            await self._spp.send(data)
        elif self._client and self._client.is_connected:
            await self._client.write_gatt_char(
                CHARACTERISTIC_UUID,
                data,
                response=False,
            )

    async def disconnect(self):
        if self._spp is not None:
            await self._spp.disconnect()
            self._spp = None
        if self._client:
            await self._client.disconnect()
            self._client = None
            logger.info("Disconnected from BLE meter")

    @property
    def is_connected(self) -> bool:
        if self._spp is not None:
            return self._spp.is_connected
        return self._client is not None and self._client.is_connected

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.disconnect()

