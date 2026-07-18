"""Classic Bluetooth RFCOMM transport for Atorch SPP power meters."""

import asyncio
import logging
import socket
import time

from atorch_protocol import MAGIC_HEADER, MessageType, REPORT_PACKET_LEN

logger = logging.getLogger(__name__)

DEFAULT_RFCOMM_CHANNELS = (1, 2)


class SppMeterConnection:
    """Read Atorch report packets from a Classic Bluetooth byte stream."""

    def __init__(
        self,
        mac_address: str,
        timeout: float = 10.0,
        channels: tuple[int, ...] = DEFAULT_RFCOMM_CHANNELS,
    ):
        self.mac_address = mac_address
        self.timeout = timeout
        self.channels = channels
        self._socket: socket.socket | None = None
        self._buffer = bytearray()

    def _connect_blocking(self) -> socket.socket:
        errors = []
        for channel in self.channels:
            sock = socket.socket(
                socket.AF_BLUETOOTH,
                socket.SOCK_STREAM,
                socket.BTPROTO_RFCOMM,
            )
            sock.settimeout(self.timeout)
            try:
                sock.connect((self.mac_address, channel))
            except OSError as exc:
                errors.append(f"channel {channel}: {exc}")
                sock.close()
                continue
            sock.settimeout(None)
            logger.info(
                "Connected to SPP meter %s on RFCOMM channel %d",
                self.mac_address,
                channel,
            )
            return sock

        detail = "; ".join(errors) or "no RFCOMM channels configured"
        raise RuntimeError(f"Could not connect to SPP meter {self.mac_address}: {detail}")

    async def connect(self) -> socket.socket:
        self._socket = await asyncio.to_thread(self._connect_blocking)
        return self._socket

    def _extract_report(self) -> bytes | None:
        header = MAGIC_HEADER + bytes([MessageType.REPORT])
        start = self._buffer.find(header)
        if start < 0:
            if len(self._buffer) > len(header) - 1:
                del self._buffer[: -(len(header) - 1)]
            return None
        if start:
            del self._buffer[:start]
        if len(self._buffer) < REPORT_PACKET_LEN:
            return None
        packet = bytes(self._buffer[:REPORT_PACKET_LEN])
        del self._buffer[:REPORT_PACKET_LEN]
        return packet

    def _read_packet_blocking(self, timeout: float | None) -> bytes:
        if self._socket is None:
            raise RuntimeError("SPP meter is not connected")

        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            packet = self._extract_report()
            if packet is not None:
                return packet

            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError("No packet received within timeout")
            self._socket.settimeout(remaining)
            try:
                chunk = self._socket.recv(1024)
            except socket.timeout as exc:
                raise TimeoutError("No packet received within timeout") from exc
            if not chunk:
                raise ConnectionError("SPP meter disconnected")
            self._buffer.extend(chunk)

    async def read_packet(self, timeout: float | None = None) -> bytes:
        return await asyncio.to_thread(self._read_packet_blocking, timeout)

    async def send(self, data: bytes):
        if self._socket is not None:
            await asyncio.to_thread(self._socket.sendall, data)

    async def disconnect(self):
        if self._socket is not None:
            sock, self._socket = self._socket, None
            await asyncio.to_thread(sock.close)
            logger.info("Disconnected from SPP meter")

    @property
    def is_connected(self) -> bool:
        return self._socket is not None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.disconnect()
