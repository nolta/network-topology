"""SNMP access, currently backed by the net-snmp CLI tools.

The whole module exists to hand the rest of the code *decoded, typed*
varbinds -- an OID, an explicit SnmpType, and (for OCTET STRINGs) the true
raw bytes. Callers ask for what they want via as_text()/as_mac() rather
than pattern-matching on how some backend chose to print a value. That
keeps the backend swappable (a pure-Python SNMP library would populate the
same structure natively) and, more importantly, keeps *interpretation*
decisions in our code where we can see them.

Why we shell out to net-snmp: it's already installed, well-tested, and
handles the wire protocol. But its text output is lossy by default -- it
applies DISPLAY-HINTs from whatever MIBs happen to be installed on the
host and guesses whether an OCTET STRING is "printable". We defeat both:

  -m ""  load no MIBs, so no DISPLAY-HINT ever reformats a value and the
         output can't vary with the host's MIB set
  -On    numeric OIDs
  -Ox    print every OCTET STRING as hex, so we always recover raw bytes
  -Oe    numeric enums, never symbolic labels

Without this, a value's printed form depends on the host's MIB
installation and on net-snmp's printability heuristic -- which is exactly
what hid a Planet quirk (LLDP chassis IDs sent as ASCII "A8-F7-..." text
rather than 6 raw bytes) behind a STRING/Hex-STRING distinction.
"""
from __future__ import annotations

import enum
import logging
import re
import subprocess
import time
from dataclasses import dataclass

from . import mibs

logger = logging.getLogger(__name__)

# Flags that make net-snmp's output deterministic and machine-readable.
# See the module docstring for why each one matters.
_OUTPUT_ARGS = ["-m", "", "-Onxe"]

_VARBIND_RE = re.compile(
    r"^(?P<oid>\.\d[\d.]*)\s*=\s*(?:(?P<type>[A-Za-z][\w-]*)\s*:\s*)?(?P<value>.*)$"
)
_NOT_HEX_RE = re.compile(r"[^0-9A-Fa-f]")


class SnmpError(RuntimeError):
    pass


class SnmpType(enum.Enum):
    OCTET_STRING = "octet-string"
    INTEGER = "integer"
    COUNTER = "counter"
    GAUGE = "gauge"
    TIMETICKS = "timeticks"
    IP_ADDRESS = "ip-address"
    OID = "oid"
    NULL = "null"
    UNKNOWN = "unknown"


# net-snmp's printed type token -> our type.
_TYPE_TOKENS = {
    "Hex-STRING": SnmpType.OCTET_STRING,
    "STRING": SnmpType.OCTET_STRING,
    "OCTET": SnmpType.OCTET_STRING,
    "INTEGER": SnmpType.INTEGER,
    "Counter32": SnmpType.COUNTER,
    "Counter64": SnmpType.COUNTER,
    "Gauge32": SnmpType.GAUGE,
    "UInteger32": SnmpType.GAUGE,
    "Timeticks": SnmpType.TIMETICKS,
    "IpAddress": SnmpType.IP_ADDRESS,
    "OID": SnmpType.OID,
    "Null": SnmpType.NULL,
}
_NUMERIC_TYPES = {SnmpType.INTEGER, SnmpType.COUNTER, SnmpType.GAUGE, SnmpType.TIMETICKS}


def _mac_from_hex_text(text: str) -> str | None:
    """'A8-F7-E0-89-A0-65' / 'a8f7e08 9a065' -> 'a8:f7:e0:89:a0:65', or None
    if it isn't 12 hex digits' worth of text."""
    hex_chars = _NOT_HEX_RE.sub("", text)
    if len(hex_chars) != 12:
        return None
    return ":".join(hex_chars[i : i + 2].lower() for i in range(0, 12, 2))


@dataclass
class SnmpVarBind:
    """One decoded varbind. `raw` holds the true bytes for OCTET STRINGs;
    `number` the value for integer-ish types. Callers use as_text()/as_mac()
    to say what they expect rather than inspecting a printed form."""

    oid: str  # numeric dotted, leading dot, e.g. ".1.3.6.1.2.1.1.5.0"
    type: SnmpType
    raw: bytes = b""
    number: int | None = None
    text_value: str = ""  # for types with no better representation (OID, IpAddress)

    @property
    def is_mac_bytes(self) -> bool:
        """True for a genuine 6-byte OCTET STRING (an ifPhysAddress etc)."""
        return self.type is SnmpType.OCTET_STRING and len(self.raw) == 6

    def as_text(self) -> str:
        if self.type is SnmpType.OCTET_STRING:
            return self.raw.decode("utf-8", errors="replace").rstrip("\x00").strip()
        if self.number is not None:
            return str(self.number)
        return self.text_value

    def as_mac(self) -> str:
        """Lowercase colon-separated MAC.

        Handles a proper 6-byte OCTET STRING, and vendors (e.g. Planet) that
        put the MAC in as ASCII text like 'A8-F7-E0-89-A0-65'. Falls back to
        as_text() if the value is neither.
        """
        if self.is_mac_bytes:
            return ":".join(f"{b:02x}" for b in self.raw)
        text = self.as_text()
        return _mac_from_hex_text(text) or text


def _index_suffix(oid: str, base_oid: str) -> str:
    """Return the index portion of oid after base_oid, e.g. '1.2.3'."""
    base = base_oid if base_oid.startswith(".") else "." + base_oid
    if not oid.startswith(base + "."):
        raise SnmpError(f"OID {oid} is not under base {base}")
    return oid[len(base) + 1 :]


def _build_varbind(oid: str, token: str, value_text: str) -> SnmpVarBind:
    kind = _TYPE_TOKENS.get(token, SnmpType.UNKNOWN)
    # net-snmp renders a zero-length OCTET STRING as a bare `= ""` with no
    # type token at all (seen on lldpRemSysName for a neighbour that sends
    # no system name). Treat a typeless quoted value as the empty string it
    # is -- otherwise it decodes to a literal '""', which is truthy and
    # silently wins over a legitimate fallback.
    if (
        kind is SnmpType.UNKNOWN
        and len(value_text) >= 2
        and value_text.startswith('"')
        and value_text.endswith('"')
    ):
        kind, token = SnmpType.OCTET_STRING, "STRING"
    if kind is SnmpType.OCTET_STRING:
        if token == "Hex-STRING":
            try:
                raw = bytes(int(b, 16) for b in value_text.split())
            except ValueError:
                raw = b""
        else:  # plain STRING (shouldn't happen under -Ox, handled defensively)
            text = value_text
            if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
                text = text[1:-1]
            raw = text.encode("utf-8", errors="replace")
        return SnmpVarBind(oid=oid, type=kind, raw=raw)
    if kind in _NUMERIC_TYPES:
        m = re.search(r"-?\d+", value_text)
        return SnmpVarBind(
            oid=oid, type=kind,
            number=int(m.group()) if m else None,
            text_value=value_text,
        )
    return SnmpVarBind(oid=oid, type=kind, text_value=value_text)


def _parse_lines(output: str) -> list[SnmpVarBind]:
    """Parse net-snmp output into varbinds. Long Hex-STRING values wrap
    across lines (16 bytes per line), so lines that aren't a new
    'OID = TYPE: value' are continuations of the previous varbind."""
    results: list[SnmpVarBind] = []
    oid = token = None
    parts: list[str] = []

    def flush():
        if oid is not None:
            results.append(_build_varbind(oid, token or "", " ".join(parts).strip()))

    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("Timeout:") or "No Such" in stripped:
            flush()
            oid = token = None
            parts = []
            continue
        m = _VARBIND_RE.match(stripped)
        if m:
            flush()
            oid = m.group("oid")
            token = m.group("type") or ""
            parts = [m.group("value").strip()]
        elif oid is not None:
            parts.append(stripped)  # continuation of a wrapped Hex-STRING
    flush()
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
        *_OUTPUT_ARGS,
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
        *_OUTPUT_ARGS,
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
        logger.debug("get %s %s OK in %.2fs: %s", host, label, elapsed, varbinds[0].as_text())
    else:
        logger.debug("get %s %s FAILED after %.2fs: no value returned", host, label, elapsed)
    return varbinds[0] if varbinds else None


def mac_from_octets(octets: list[str]) -> str:
    """Format decimal OID index octets ('0','10','92',...) as a MAC. Used
    for MACs that arrive as part of an OID index rather than as a value."""
    return ":".join(f"{int(o):02x}" for o in octets)
