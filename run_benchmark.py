"""Run a Phoronix benchmark on a remote host with synchronized power logging.

Usage:
    python run_benchmark.py <inventory-host> <test> [options]
    python run_benchmark.py node2 local/power-bench-build-kernel-defconfig-1.0.0 \
        --db benchmarks/power_meter.duckdb \
        --optimization baseline --repeat 1 --inventory ansible/hosts \
        --mac 45:AF:4E:55:56:06 --checksum-policy warn --reboot
    python run_benchmark.py node2 --idle-only --optimization baseline \
        --inventory ansible/hosts --mac 45:AF:4E:55:56:06 --checksum-policy warn

Each run moves through the phases settle -> idle -> bench -> cooldown. Post-boot
settling samples are tagged 'settle'; the window flips to 'idle' only once power is
stable (rolling stdev below --idle-stable-w), so idle averages never include a
machine that is still settling. Energy-to-complete is integrated from the
bench-phase power samples — the meter's energy counter only ticks in 10 Wh steps
and its delta is stored purely as a cross-check. The Phoronix result is retrieved
afterwards (every result entry into run_results, the first into runs.bench_score),
and every reading is tagged with its phase and run_id, so performance-per-joule can
be computed from the database.

With --idle-only the run stops after a long stable idle window (no benchmark) —
the dedicated idle-power measurement for idle-targeted knobs.
"""

import argparse
import asyncio
import json
import logging
import os
import shlex
import statistics
import subprocess
import sys
import time
import uuid
from collections import deque

from ansible_inventory import InventoryHostError, resolve_inventory_host
from database import Database
from meter_ble import MeterConnection
from atorch_protocol import parse_report, verify_checksum, MAGIC_HEADER, MessageType
from pts_results import parse_composite_xml

logger = logging.getLogger("run_benchmark")

DEFAULT_SETTLE_SECONDS = 30
METER_FIRST_READING_TIMEOUT = 15.0
READ_PACKET_TIMEOUT = 5.0
IDLE_STDEV_WINDOW = 15          # samples in the rolling window for the stability gate
THERMAL_GATE_TIMEOUT = 300.0
THERMAL_POLL_SECONDS = 10.0
MIN_SAMPLE_COVERAGE = 0.9       # below this fraction of expected samples a run is invalid


class LoggerState:
    """Shared state between the power-logger task and the main coroutine."""

    def __init__(self):
        self.phase = "settle"
        self.latest = None          # most recent MeterReading
        self.valid = 0
        self.dropped = 0
        self.checksum_failures = 0
        self.first_reading = asyncio.Event()
        self.recent_power = deque(maxlen=IDLE_STDEV_WINDOW)  # rolling window, idle gate

    def latest_energy(self):
        return self.latest.energy if self.latest is not None else None

class RunBuffer:
    """In-memory representation of one run, persisted only after completion."""

    def __init__(self, **fields):
        self.fields = fields
        self.readings = []
        self.results = []

    def update_run(self, _run_id=None, **fields):
        self.fields.update(fields)

    def insert(self, reading, run_id=None, phase=None):
        self.readings.append((reading, phase))

    def phase_readings(self, phase):
        return [reading for reading, reading_phase in self.readings
                if reading_phase == phase]


def ssh_command(host: str, cmd: str, user: str | None = None, key: str | None = None) -> list[str]:
    args = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]
    if key:
        args += ["-i", key]
    target = f"{user}@{host}" if user else host
    args += [target, cmd]
    return args


def reboot_host(host: str, user: str | None = None, key: str | None = None, timeout: float = 300.0) -> bool:
    """Reboot the target and block until SSH answers again, so the next test starts fresh.

    A fresh boot is the reset mechanism for the sweep: every runtime knob is applied
    non-persistently, so rebooting returns the host to its baseline configuration.
    """
    logger.info("Rebooting %s (end of test)...", host)
    # The reboot drops the SSH session; a non-zero return code here is expected.
    try:
        subprocess.run(ssh_command(host, "sudo reboot", user, key),
                       capture_output=True, text=True, timeout=30)
    except subprocess.SubprocessError:
        pass

    deadline = time.time() + timeout

    def ssh_probe() -> bool:
        try:
            r = subprocess.run(
                ssh_command(host, "true", user, key),
                capture_output=True,
                text=True,
                timeout=15,
            )
            return r.returncode == 0
        except subprocess.SubprocessError:
            return False

    # Wait for the host to actually go down (stop answering SSH).
    while time.time() < deadline:
        if not ssh_probe():
            break
        time.sleep(3)
    # Wait for it to come back up.
    logger.info("Waiting for %s to come back up...", host)
    while time.time() < deadline:
        if ssh_probe():
            logger.info("%s is back up", host)
            return True
        time.sleep(5)
    logger.warning("Timed out waiting for %s to return after reboot", host)
    return False


def gather_host_info(host: str, user: str | None = None, key: str | None = None) -> dict:
    """Best-effort post-apply snapshot of the host's power-relevant configuration.

    The kernel/cpu_model/memory_bytes/governor/turbo keys land in their own ``runs`` columns; the
    full dict is stored as JSON in ``runs.applied_config`` so a silently failed apply
    (best-effort sysfs writes) is detectable after the fact.
    """
    script = (
        'echo "kernel=$(uname -r)"; '
        "echo \"cpu=$(LC_ALL=C lscpu | sed -n 's/^Model name:[[:space:]]*//p' | head -1)\"; "
        "echo \"memory_bytes=$(awk '/MemTotal:/{printf \"%.0f\", $2 * 1024}' /proc/meminfo)\"; "
        'echo "governor=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)"; '
        # intel_pstate no_turbo: 0 = turbo on. cpufreq boost (AMD/acpi): 1 = turbo on
        # (inverted sense) — normalize both to on/off here.
        'if [ -e /sys/devices/system/cpu/intel_pstate/no_turbo ]; then '
        '[ "$(cat /sys/devices/system/cpu/intel_pstate/no_turbo)" = "0" ] && echo "turbo=on" || echo "turbo=off"; '
        'elif [ -e /sys/devices/system/cpu/cpufreq/boost ]; then '
        '[ "$(cat /sys/devices/system/cpu/cpufreq/boost)" = "1" ] && echo "turbo=on" || echo "turbo=off"; '
        'fi; '
        'echo "driver=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_driver 2>/dev/null)"; '
        'echo "epp=$(cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference 2>/dev/null)"; '
        'echo "aspm=$(cat /sys/module/pcie_aspm/parameters/policy 2>/dev/null)"; '
        'for q in /sys/block/*/queue/scheduler; do d=$(echo "$q" | cut -d/ -f4); '
        'case "$d" in loop*|ram*|zram*|dm-*|sr*) continue;; esac; '
        "echo \"io=$d:$(sed -n 's/.*\\[\\(.*\\)\\].*/\\1/p' \"$q\")\"; break; done; "
        'echo "cmdline=$(cat /proc/cmdline)"'
    )
    field_map = {"kernel": "kernel", "cpu": "cpu_model", "memory_bytes": "memory_bytes", "governor": "governor",
                 "turbo": "turbo", "driver": "scaling_driver", "epp": "epp",
                 "aspm": "aspm_policy", "io": "io_scheduler", "cmdline": "cmdline"}
    info = {column: None for column in field_map.values()}
    try:
        r = subprocess.run(
            ssh_command(host, script, user, key),
            capture_output=True, text=True, timeout=15,
        )
        for line in r.stdout.splitlines():
            key_, _, value = line.partition("=")
            if key_ in field_map:
                info[field_map[key_]] = value.strip() or None
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("Could not gather host info: %s", exc)
    return info


def read_host_cpu_temp(host: str, user: str | None = None, key: str | None = None) -> float | None:
    """Max CPU temperature across the host's thermal zones, in C. None if unreadable."""
    cmd = "cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | sort -n | tail -1"
    try:
        r = subprocess.run(ssh_command(host, cmd, user, key),
                           capture_output=True, text=True, timeout=15)
        value = r.stdout.strip()
        return int(value) / 1000.0 if value else None
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def launch_benchmark(host: str, test: str, result_name: str,
                     user: str | None = None, key: str | None = None) -> subprocess.Popen:
    """Launch PTS non-interactively with default profile options and a named result."""
    env = (
        f"TEST_RESULTS_NAME={shlex.quote(result_name)} "
        f"TEST_RESULTS_IDENTIFIER={shlex.quote(result_name)} "
        f"TEST_RESULTS_DESCRIPTION={shlex.quote('power_bench automated run')} "
        f"FORCE_TIMES_TO_RUN=1 "
        f"PTS_SILENT_MODE=1 "
    )
    run_cmd = f"{env}phoronix-test-suite default-run {shlex.quote(test)}"
    return subprocess.Popen(
        ssh_command(host, run_cmd, user, key),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def pts_result_name_candidates(result_name: str) -> list[str]:
    """PTS sanitizes saved result directory names; try the common variants."""
    candidates = [result_name]
    candidates.append(result_name.lower())
    candidates.append("".join(c for c in result_name.lower() if c.isalnum()))
    candidates.append("".join(c for c in result_name.lower() if c.isalnum() or c in "-."))
    deduped = []
    for candidate in candidates:
        if candidate and candidate not in deduped:
            deduped.append(candidate)
    return deduped


def fetch_pts_result(host: str, result_name: str,
                     user: str | None = None, key: str | None = None):
    """Pull the composite.xml for a result name over SSH and parse it.

    Returns ``(results, xml_text)`` — ``([], None)`` if the file could not be
    fetched, ``([], xml_text)`` if it was fetched but not parseable (so the raw
    XML can still be archived for debugging).
    """
    tried = []
    for candidate in pts_result_name_candidates(result_name):
        tried.append(candidate)
        remote = f"$HOME/.phoronix-test-suite/test-results/{shlex.quote(candidate)}/composite.xml"
        try:
            r = subprocess.run(
                ssh_command(host, f"cat {remote}", user, key),
                capture_output=True, text=True, timeout=30,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning("Failed to fetch PTS result: %s", exc)
            return [], None
        if r.returncode == 0 and r.stdout.strip():
            if candidate != result_name:
                logger.info("PTS result '%s' found as sanitized name '%s'",
                            result_name, candidate)
            break
    else:
        logger.warning("PTS result file not found for '%s' (tried: %s)",
                       result_name, ", ".join(tried))
        return [], None
    try:
        return parse_composite_xml(r.stdout), r.stdout
    except Exception as exc:  # malformed XML
        logger.warning("Could not parse PTS result XML: %s", exc)
        return [], r.stdout


def archive_pts_xml(db_path: str, result_name: str, xml_text: str):
    """Keep the raw composite.xml next to the DB for provenance."""
    out_dir = os.path.join(os.path.dirname(db_path) or ".", "pts_results")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{result_name}.xml")
    with open(path, "w") as f:
        f.write(xml_text)
    logger.info("Archived PTS result XML to %s", path)


async def power_logger(run: RunBuffer, interval: float,
                       conn: MeterConnection, state: LoggerState, stop_event: asyncio.Event):
    seq = 0
    last_log = 0.0
    while not stop_event.is_set():
        try:
            raw = await conn.read_packet(timeout=READ_PACKET_TIMEOUT)
        except TimeoutError:
            continue

        # Only report packets matter; other message types (replies) aren't corruption.
        if len(raw) < 4 or raw[0:2] != MAGIC_HEADER or raw[2] != MessageType.REPORT:
            continue

        if not verify_checksum(raw):
            state.checksum_failures += 1
            if state.checksum_failures == 1 and state.phase == "settle":
                logger.warning(
                    "Meter checksum mismatch; policy=%s",
                    state.checksum_policy,
                )
            if state.checksum_policy == "strict":
                state.dropped += 1
                continue

        now = time.time()
        if now - last_log < interval:
            continue
        last_log = now

        try:
            reading = parse_report(raw, now)
        except ValueError:
            state.dropped += 1
            continue

        state.latest = reading
        state.recent_power.append(reading.power)
        if not state.first_reading.is_set():
            state.first_reading.set()

        run.insert(reading, phase=state.phase)
        state.valid += 1
        seq += 1
        logger.info(
            "#%d [%s] %.1fV %.3fA %.2fW %.1fC",
            seq, state.phase, reading.voltage, reading.current,
            reading.power, reading.temperature,
        )
    logger.info("Power logging stopped: %d readings, %d dropped", state.valid, state.dropped)


async def stream_remote_output(proc: subprocess.Popen):
    """Drain remote stdout in an executor so the event loop (and power sampling) keeps running.

    ``readline()`` is a blocking call; running it inline on the event loop would stall
    the power-logger task during quiet stretches of a long benchmark.
    """
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, proc.stdout.readline)
        if line == "":          # EOF -> process finished
            break
        logger.info("[remote] %s", line.rstrip())
    proc.wait()


def _rolling_stdev(values) -> float | None:
    return statistics.stdev(values) if len(values) >= 2 else None


async def wait_for_idle_stability(state: LoggerState, args: argparse.Namespace):
    """Hold the settle phase until power is stable or the timeout expires."""
    logger.info(
        "Settling for %ss minimum (gate: stdev of last %d samples < %.2f W)...",
        args.settle,
        IDLE_STDEV_WINDOW,
        args.idle_stable_w,
    )
    await asyncio.sleep(args.settle)
    deadline = time.time() + args.idle_timeout
    while True:
        window = list(state.recent_power)
        stdev = _rolling_stdev(window)
        if (
            len(window) >= IDLE_STDEV_WINDOW
            and stdev is not None
            and stdev < args.idle_stable_w
        ):
            logger.info("Idle stable (stdev %.3f W)", stdev)
            return
        if time.time() > deadline:
            logger.warning(
                "Idle stability not reached within %ss (stdev %s); proceeding anyway",
                args.idle_timeout,
                "n/a" if stdev is None else f"{stdev:.3f} W",
            )
            return
        await asyncio.sleep(2.0)


async def thermal_gate(args: argparse.Namespace) -> float | None:
    """Read and optionally gate on CPU temperature before the benchmark."""
    loop = asyncio.get_running_loop()
    temp = await loop.run_in_executor(
        None,
        read_host_cpu_temp,
        args.connection_host,
        args.connection_user,
        args.connection_key,
    )
    if temp is None:
        logger.warning("Thermal gate: could not read host CPU temperature")
        return None
    if args.cool_to is not None:
        deadline = time.time() + THERMAL_GATE_TIMEOUT
        while temp is not None and temp > args.cool_to:
            if time.time() > deadline:
                logger.warning(
                    "Thermal gate: still %.1fC > %.1fC after %.0fs; proceeding",
                    temp,
                    args.cool_to,
                    THERMAL_GATE_TIMEOUT,
                )
                break
            logger.info(
                "Thermal gate: %.1fC > %.1fC, waiting...",
                temp,
                args.cool_to,
            )
            await asyncio.sleep(THERMAL_POLL_SECONDS)
            temp = await loop.run_in_executor(
                None,
                read_host_cpu_temp,
                args.connection_host,
                args.connection_user,
                args.connection_key,
            )
        if temp is not None and temp <= args.cool_to:
            logger.info(
                "Thermal gate: %.1fC <= %.1fC, proceeding",
                temp,
                args.cool_to,
            )
    return temp


def finalize_bench_metrics(
    run: RunBuffer, bench_start: float, bench_end: float, interval: float
):
    """Calculate integrated energy and coverage from buffered bench samples."""
    rows = sorted(
        ((reading.timestamp, reading.power) for reading in run.phase_readings("bench")),
        key=lambda row: row[0],
    )
    energy_wh = None
    if len(rows) >= 2:
        energy_wh = sum(
            (p0 + p1) / 2.0 * (t1 - t0)
            for (t0, p0), (t1, p1) in zip(rows, rows[1:])
        ) / 3600.0
    duration = bench_end - bench_start
    coverage = len(rows) / (duration / interval) if duration > 0 else None
    run.update_run(
        energy_wh_integrated=energy_wh,
        bench_sample_coverage=coverage,
    )
    if coverage is not None and coverage < MIN_SAMPLE_COVERAGE:
        logger.warning(
            "Bench sample coverage %.0f%% < %.0f%% — completed run is INVALID",
            coverage * 100,
            MIN_SAMPLE_COVERAGE * 100,
        )


async def run_async(args: argparse.Namespace, run: RunBuffer) -> int:
    state = LoggerState()
    state.checksum_policy = args.checksum_policy
    stop_event = asyncio.Event()
    conn = MeterConnection(mac_address=args.mac, timeout=args.timeout)

    logger.info("Connecting to power meter...")
    await conn.connect()
    logger_task = asyncio.create_task(
        power_logger(run, args.interval, conn, state, stop_event)
    )
    proc = None
    try:
        try:
            await asyncio.wait_for(
                state.first_reading.wait(), METER_FIRST_READING_TIMEOUT
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"No valid meter reading within {METER_FIRST_READING_TIMEOUT:.0f}s; "
                "aborting before benchmark"
            )

        if args.ambient is None and state.latest is not None:
            run.update_run(ambient_c=state.latest.temperature)
            logger.info(
                "Ambient auto-filled from meter temperature: %.1f C",
                state.latest.temperature,
            )

        state.phase = "settle"
        await wait_for_idle_stability(state, args)

        state.phase = "idle"
        run.update_run(idle_start=time.time())
        idle_hold = args.idle_duration if args.idle_only else args.settle
        logger.info("Idle window for %ss...", idle_hold)
        await asyncio.sleep(idle_hold)

        if args.idle_only:
            logger.info("Idle-only run complete")
            return 0

        temp_c = await thermal_gate(args)
        if temp_c is not None:
            run.update_run(bench_start_temp_c=temp_c)
        state.phase = "bench"
        bench_start = time.time()
        run.update_run(
            bench_start=bench_start,
            energy_wh_bench_start=state.latest_energy(),
        )
        logger.info("Launching benchmark '%s' on %s ...", args.test, args.host)
        proc = launch_benchmark(
            args.connection_host,
            args.test,
            args.result_name,
            args.connection_user,
            args.connection_key,
        )
        await stream_remote_output(proc)
        bench_end = time.time()
        run.update_run(
            bench_end=bench_end,
            energy_wh_bench_end=state.latest_energy(),
        )
        logger.info("Benchmark finished (exit %d)", proc.returncode)
        finalize_bench_metrics(run, bench_start, bench_end, args.interval)

        state.phase = "cooldown"
        logger.info("Cooldown for %ss...", args.settle)
        await asyncio.sleep(args.settle)
    finally:
        stop_event.set()
        await logger_task
        await conn.disconnect()
        run.update_run(
            dropped_packets=state.dropped,
            checksum_failures=state.checksum_failures,
        )

    return proc.returncode if proc is not None else 0


def _fmt(value):
    return "n/a" if value is None else "%.3f" % value


def print_summary(run: RunBuffer, run_id: int):
    idle = run.phase_readings("idle")
    bench = run.phase_readings("bench")
    fields = run.fields

    idle_avg = statistics.fmean(reading.power for reading in idle) if idle else None
    bench_avg = statistics.fmean(reading.power for reading in bench) if bench else None
    bench_peak = max((reading.power for reading in bench), default=None)
    e_start = fields.get("energy_wh_bench_start")
    e_end = fields.get("energy_wh_bench_end")
    e_int = fields.get("energy_wh_integrated")
    b_start = fields.get("bench_start")
    b_end = fields.get("bench_end")
    temp_c = fields.get("bench_start_temp_c")
    coverage = fields.get("bench_sample_coverage")
    score = fields.get("bench_score")
    unit = fields.get("bench_unit")
    higher_is_better = fields.get("higher_is_better")
    dropped = fields.get("dropped_packets")
    checksum_failures = fields.get("checksum_failures")

    counter_wh = (
        e_end - e_start if e_start is not None and e_end is not None else None
    )
    duration_s = (
        b_end - b_start if b_start is not None and b_end is not None else None
    )

    lines = [f"=== Run #{run_id} Summary ==="]
    lines.append(f"  Idle power : {_fmt(idle_avg)} W  ({len(idle)} samples)")
    if bench:
        lines.append(
            f"  Load power : {_fmt(bench_avg)} W avg, {_fmt(bench_peak)} W peak  "
            f"({len(bench)} samples)"
        )
    if duration_s is not None:
        lines.append(f"  Bench time : {duration_s:.1f} s")
    if temp_c is not None:
        lines.append(f"  Start temp : {temp_c:.1f} C (CPU, thermal gate)")
    if e_int is not None:
        lines.append(
            f"  Energy     : {e_int:.3f} Wh to complete (integrated; "
            f"counter delta {_fmt(counter_wh)} Wh @ 10 Wh/tick)"
        )
    if coverage is not None:
        flag = "" if coverage >= MIN_SAMPLE_COVERAGE else "  << INVALID, re-run"
        lines.append(
            f"  Coverage   : {coverage * 100:.0f}% of expected bench samples{flag}"
        )
    if score is not None:
        direction = "higher=better" if higher_is_better else "lower=better"
        lines.append(f"  Score      : {score:.4f} {unit or ''} ({direction})")
        if higher_is_better and e_int:
            lines.append(
                f"  Perf/Wh    : {score / e_int:.4f} {unit or 'units'} per Wh"
            )
    lines.append(f"  Dropped    : {dropped or 0} packets")
    if checksum_failures:
        lines.append(f"  Checksums  : {checksum_failures} failed frame checks")
    logger.info("\n".join(lines))


def run(args: argparse.Namespace):
    info = gather_host_info(
        args.connection_host,
        args.connection_user,
        args.connection_key,
    )
    args.result_name = None
    if not args.idle_only:
        safe_host = "".join(
            character
            for character in args.host
            if character.isalnum() or character in "-_"
        )
        args.result_name = f"power_bench_{safe_host}_{uuid.uuid4().hex[:12]}"

    run_buffer = RunBuffer(
        host=args.host,
        test=args.test,
        optimization=args.optimization,
        repeat_idx=args.repeat,
        config_hash=args.config_hash,
        ambient_c=args.ambient,
        applied_config=json.dumps(info),
        result_name=args.result_name,
        **info,
    )
    logger.info(
        "Starting test=%s optimization=%s repeat=%d on inventory host %s",
        args.test,
        args.optimization,
        args.repeat,
        args.host,
    )

    try:
        rc = asyncio.run(run_async(args, run_buffer))
    except Exception as exc:
        logger.error(
            "Run aborted before completion; nothing was written to DuckDB: %s", exc
        )
        if args.reboot:
            reboot_host(
                args.connection_host,
                args.connection_user,
                args.connection_key,
            )
        raise SystemExit(2)

    if rc != 0:
        logger.error(
            "Benchmark exited %d; incomplete run was not written to DuckDB",
            rc,
        )
        if args.reboot:
            reboot_host(
                args.connection_host,
                args.connection_user,
                args.connection_key,
            )
        raise SystemExit(rc)

    if not args.idle_only:
        results, xml_text = fetch_pts_result(
            args.connection_host,
            args.result_name,
            args.connection_user,
            args.connection_key,
        )
        if xml_text:
            archive_pts_xml(args.db, args.result_name, xml_text)
        if not results:
            logger.error("No PTS result found; incomplete run was not written to DuckDB")
            if args.reboot:
                reboot_host(
                    args.connection_host,
                    args.connection_user,
                    args.connection_key,
                )
            raise SystemExit(1)

        run_buffer.results = results
        primary = results[0]
        run_buffer.update_run(
            bench_score=primary.value,
            bench_unit=primary.scale,
            higher_is_better=primary.higher_is_better,
        )
        logger.info(
            "Buffered result: %.4f %s (%s)",
            primary.value,
            primary.scale,
            "higher=better" if primary.higher_is_better else "lower=better",
        )

    try:
        with Database(args.db) as database:
            run_id = database.persist_run(
                run_buffer.fields,
                run_buffer.readings,
                run_buffer.results,
            )
    except Exception as exc:
        logger.error("Could not persist completed run: %s", exc)
        if args.reboot:
            reboot_host(
                args.connection_host,
                args.connection_user,
                args.connection_key,
            )
        raise SystemExit(2)

    logger.info("Persisted completed run #%d", run_id)
    print_summary(run_buffer, run_id)

    if args.reboot:
        reboot_host(
            args.connection_host,
            args.connection_user,
            args.connection_key,
        )
    raise SystemExit(0)


def main():
    parser = argparse.ArgumentParser(
        description="Run a Phoronix benchmark on one Ansible inventory host"
    )
    parser.add_argument(
        "host",
        help="Ansible inventory host name (literal IP addresses are rejected)",
    )
    parser.add_argument(
        "test",
        nargs="?",
        default=None,
        help="Phoronix test/profile/suite; omit with --idle-only",
    )
    parser.add_argument("--inventory", default="ansible/hosts")
    parser.add_argument(
        "--db",
        "-d",
        default="benchmarks/power_meter.duckdb",
        help="DuckDB database path",
    )
    parser.add_argument("--optimization", "-o", default="baseline")
    parser.add_argument("--repeat", "-r", type=int, default=1)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--settle", "-s", type=float, default=DEFAULT_SETTLE_SECONDS)
    parser.add_argument("--idle-only", action="store_true")
    parser.add_argument("--idle-duration", type=float, default=600.0)
    parser.add_argument("--idle-stable-w", type=float, default=1.0)
    parser.add_argument("--idle-timeout", type=float, default=300.0)
    parser.add_argument("--cool-to", type=float, default=None)
    parser.add_argument("--ambient", type=float, default=None)
    parser.add_argument("--config-hash", dest="config_hash", default=None)
    parser.add_argument("--mac", "-m", default=None)
    parser.add_argument("--timeout", "-t", type=float, default=10.0)
    parser.add_argument(
        "--checksum-policy",
        choices=["strict", "warn"],
        default="strict",
    )
    parser.add_argument("--verbose", "-V", action="store_true")
    parser.add_argument(
        "--reboot",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    args = parser.parse_args()

    if args.idle_only:
        args.test = "idle"
    elif not args.test:
        parser.error("test is required unless --idle-only is given")

    try:
        inventory_host = resolve_inventory_host(args.host, args.inventory)
    except InventoryHostError as exc:
        parser.error(str(exc))
    args.connection_host = inventory_host.address
    args.connection_user = inventory_host.user
    args.connection_key = inventory_host.private_key

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    run(args)


if __name__ == "__main__":
    main()
