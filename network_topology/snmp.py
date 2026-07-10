"""Thin wrapper around the net-snmp CLI tools (snmpget/snmpbulkwalk).

We shell out rather than use a Python SNMP library because net-snmp's CLI
tools are already installed, well-tested, and avoid any dependency on MIB
files being present -- we always request numeric OIDs (-On) and decode
values ourselves.
"""
from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass

from . import mibs

logger = logging.getLogger(__name__)

_LINE_RE = re.compile(
    r"^(?P<oid>\.\d[\d.]*)\s*=\s*(?:(?P<type>[A-Za-z][\w-]*)\s*:\s*)?(?P<value>.*)$"
)


class SnmpError(RuntimeError):
    pass


@dataclass
class SnmpVarBind:
    oid: str  # dotted, leading dot, e.g. ".1.3.6.1.2.1.1.5.0"
    type: str  # e.g. "STRING", "Hex-STRING", "INTEGER", "Timeticks"
    value: str  # raw textual value, quotes stripped for STRING


def _index_suffix(oid: str, base_oid: str) -> str:
    """Return the index portion of oid after base_oid, e.g. '1.2.3'."""
    base = base_oid if base_oid.startswith(".") else "." + base_oid
    if not oid.startswith(base + "."):
        raise SnmpError(f"OID {oid} is not under base {base}")
    return oid[len(base) + 1 :]


def _parse_lines(output: str) -> list[SnmpVarBind]:
    results = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("Timeout:") or "No Such" in line:
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        value = m.group("value").strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        results.append(
            SnmpVarBind(oid=m.group("oid"), type=m.group("type") or "", value=value)
        )
    return results


def walk(
    host: str,
    community: str,
    base_oid: str,
    version: str = "2c",
    timeout: int = 3,
    retries: int = 1,
    max_repetitions: int = 10,
) -> dict[str, SnmpVarBind]:
    """Walk base_oid on host, return {index_suffix: SnmpVarBind}.

    max_repetitions controls GETBULK's row count per PDU (-Cr). Weak
    embedded agents can choke assembling large bulk responses; lowering
    this trades more round-trips for a better chance each one succeeds.
    """
    cmd = [
        "snmpbulkwalk",
        "-v",
        version,
        "-c",
        community,
        "-On",
        "-t",
        str(timeout),
        "-r",
        str(retries),
        f"-Cr{max_repetitions}",
        host,
        base_oid,
    ]
    label = mibs.name_for(base_oid)
    logger.debug(
        "walk %s %s (max_repetitions=%d timeout=%ds retries=%d)",
        host, label, max_repetitions, timeout, retries,
    )
    start = time.monotonic()

    # Safety net for the whole walk (many GETBULK round-trips over a
    # possibly high-latency link), not just one PDU -- but bounded so a
    # single stuck table can't silently eat minutes.
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout * (retries + 1) * 8 + 10)
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - start
        logger.debug("walk %s %s FAILED after %.2fs: subprocess safety timeout", host, label, elapsed)
        raise SnmpError(f"snmpbulkwalk timed out talking to {host}") from exc

    elapsed = time.monotonic() - start
    if proc.returncode != 0 and not proc.stdout.strip():
        logger.debug("walk %s %s FAILED after %.2fs: %s", host, label, elapsed, proc.stderr.strip())
        raise SnmpError(f"snmpbulkwalk failed for {host} {base_oid}: {proc.stderr.strip()}")

    varbinds = _parse_lines(proc.stdout)
    result = {_index_suffix(vb.oid, base_oid): vb for vb in varbinds}
    logger.debug("walk %s %s OK in %.2fs (%d rows)", host, label, elapsed, len(result))
    return result


def get(
    host: str,
    community: str,
    oid: str,
    version: str = "2c",
    timeout: int = 3,
    retries: int = 1,
) -> SnmpVarBind | None:
    cmd = [
        "snmpget",
        "-v",
        version,
        "-c",
        community,
        "-On",
        "-t",
        str(timeout),
        "-r",
        str(retries),
        host,
        oid,
    ]
    label = mibs.name_for(oid)
    logger.debug("get %s %s (timeout=%ds retries=%d)", host, label, timeout, retries)
    start = time.monotonic()

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout * (retries + 1) * 8 + 10)
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - start
        logger.debug("get %s %s FAILED after %.2fs: subprocess safety timeout", host, label, elapsed)
        raise SnmpError(f"snmpget timed out talking to {host}") from exc

    elapsed = time.monotonic() - start
    varbinds = _parse_lines(proc.stdout)
    if varbinds:
        logger.debug("get %s %s OK in %.2fs: %s", host, label, elapsed, varbinds[0].value)
    else:
        logger.debug("get %s %s FAILED after %.2fs: no value returned", host, label, elapsed)
    return varbinds[0] if varbinds else None


def hexstring_to_mac(value: str) -> str:
    """Normalize a net-snmp Hex-STRING value ('AA BB CC DD EE FF') to
    lowercase colon-separated MAC ('aa:bb:cc:dd:ee:ff')."""
    parts = value.split()
    return ":".join(p.lower().zfill(2) for p in parts)


def normalize_mac(value: str) -> str:
    """Normalize any reasonable MAC representation to lowercase
    colon-separated form. Handles net-snmp's Hex-STRING output
    ('AA BB CC DD EE FF') as well as vendors (e.g. Planet) that encode
    LLDP chassis/port IDs as literal ASCII text like 'A8-F7-E0-89-A0-65'
    instead of raw MAC bytes. Returns the input unchanged if it doesn't
    look like a MAC address."""
    hex_chars = re.sub(r"[^0-9A-Fa-f]", "", value)
    if len(hex_chars) != 12:
        return value
    return ":".join(hex_chars[i : i + 2].lower() for i in range(0, 12, 2))
