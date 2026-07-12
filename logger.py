"""Main data logger: connects to an Atorch BLE meter and writes readings to DuckDB."""

import argparse
import asyncio
import logging
import signal
import time

from atorch_protocol import MeterReading, parse_report, verify_checksum
from database import Database
from meter_ble import MeterConnection

logger = logging.getLogger(__name__)

running = True


def handle_signal(sig, frame):
    global running
    running = False
    logger.info("Shutdown signal received")


async def log_loop(db_path: str, interval: float, mac: str | None, timeout: float):
    global running
    db = Database(db_path)
    db.open()
    logger.info("Database opened: %s", db_path)

    conn = MeterConnection(mac_address=mac, timeout=timeout)

    try:
        await conn.connect()
        seq = 0
        last_log = 0.0

        while running:
            try:
                raw = await conn.read_packet(timeout=5.0)
            except TimeoutError:
                logger.warning("No packets for 5s, still waiting...")
                continue

            if len(raw) < 4 or raw[0:2] != b"\xff\x55" or raw[2] != 0x01:
                continue

            if not verify_checksum(raw):
                logger.debug("Checksum mismatch (device may use different algo), proceeding anyway")

            now = time.time()
            if now - last_log < interval:
                continue
            last_log = now

            try:
                reading = parse_report(raw, now)
            except ValueError as e:
                logger.debug("Parse error: %s", e)
                continue

            db.insert(reading)
            seq += 1
            logger.info(
                "#%d  %.1fV  %.3fA  %.2fW  %.1fC  %dh%02dm%02ds",
                seq,
                reading.voltage,
                reading.current,
                reading.power,
                reading.temperature,
                reading.duration_hours,
                reading.duration_minutes,
                reading.duration_seconds,
            )
    finally:
        await conn.disconnect()
        db.close()
        logger.info("Stopped after %d readings", seq)


def main():
    global running
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    parser = argparse.ArgumentParser(description="Atorch BLE power meter data logger")
    parser.add_argument("-d", "--database", default="power_meter.duckdb", help="DuckDB file path")
    parser.add_argument("-i", "--interval", type=float, default=1.0, help="Min seconds between logged readings")
    parser.add_argument("-m", "--mac", default=None, help="BLE MAC address (auto-scan if omitted)")
    parser.add_argument("-t", "--timeout", type=float, default=10.0, help="BLE connection timeout in seconds")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    asyncio.run(log_loop(args.database, args.interval, args.mac, args.timeout))


if __name__ == "__main__":
    main()
