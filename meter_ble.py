"""BLE connection handler for Atorch power meters."""

import asyncio
import logging
import time

from bleak import BleakClient, BleakScanner

logger = logging.getLogger(__name__)

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
CHARACTERISTIC_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
DEVICE_NAME_PREFIXES = ("UD18", "AT24", "J7", "DL24")

def is_atorch_device(device) -> bool:
    """Return whether a BLE device name matches a supported Atorch family."""
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
    def __init__(self, mac_address: str | None = None, timeout: float = 10.0):
        self.mac_address = mac_address
        self.timeout = timeout
        self._client: BleakClient | None = None
        self._notify_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)

    async def _notification_handler(self, sender, data: bytearray):
        self._notify_queue.put_nowait(bytes(data))

    async def connect(self) -> BleakClient:
        if self.mac_address:
            logger.info("Connecting to %s", self.mac_address)
            self._client = BleakClient(self.mac_address, timeout=self.timeout)
        else:
            logger.info("Scanning for Atorch meter...")
            device = await BleakScanner.find_device_by_filter(
                lambda discovered, _: is_atorch_device(discovered),
                timeout=self.timeout,
            )
            if device is None:
                raise RuntimeError("No Atorch meter found during scan")
            logger.info("Found %s (%s)", device.name, device.address)
            self._client = BleakClient(device, timeout=self.timeout)

        await self._client.connect()
        await self._client.start_notify(CHARACTERISTIC_UUID, self._notification_handler)
        logger.info("Connected and notifications started")
        return self._client

    async def read_packet(self, timeout: float | None = None) -> bytes:
        try:
            return await asyncio.wait_for(self._notify_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError("No packet received within timeout")

    async def send(self, data: bytes):
        if self._client and self._client.is_connected:
            await self._client.write_gatt_char(CHARACTERISTIC_UUID, data, response=False)

    async def disconnect(self):
        if self._client:
            await self._client.disconnect()
            logger.info("Disconnected")

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.disconnect()
