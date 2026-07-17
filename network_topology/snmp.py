"""SNMP access via a small built-in SNMPv2c engine (see _snmpv2c.py).

The whole module exists to hand the rest of the code *decoded, typed*
varbinds -- an OID, an explicit SnmpType, and (for OCTET STRINGs) the true
raw bytes. Callers ask for what they want via as_text()/as_mac() rather
than inspecting how some backend chose to print a value. That keeps
*interpretation* decisions in our code where we can see them -- which is
exactly what a print-based backend hid (a Planet quirk: LLDP chassis IDs
sent as ASCII "A8-F7-..." text rather than 6 raw bytes).

We speak SNMPv2c ourselves over UDP (_snmpv2c) rather than shelling out to
the net-snmp CLI tools, so the tool has no external runtime dependency and
works on hosts that don't have net-snmp installed. v2c has no crypto, so
the engine is small.
"""
from __future__ import annotations

import enum
import logging
import re
import time
from dataclasses import dataclass

from . import _snmpv2c, mibs

logger = logging.getLogger(__name__)

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


# BER value tag -> our SnmpType.
_TAG_TO_TYPE = {
    _snmpv2c.TAG_OCTET_STRING: SnmpType.OCTET_STRING,
    _snmpv2c.TAG_OPAQUE: SnmpType.OCTET_STRING,
    _snmpv2c.TAG_INTEGER: SnmpType.INTEGER,
    _snmpv2c.TAG_COUNTER32: SnmpType.COUNTER,
    _snmpv2c.TAG_COUNTER64: SnmpType.COUNTER,
    _snmpv2c.TAG_GAUGE32: SnmpType.GAUGE,
    _snmpv2c.TAG_TIMETICKS: SnmpType.TIMETICKS,
    _snmpv2c.TAG_IPADDRESS: SnmpType.IP_ADDRESS,
    _snmpv2c.TAG_OID: SnmpType.OID,
}


def _varbind_from_raw(oid: str, tag: int, value) -> SnmpVarBind:
    kind = _TAG_TO_TYPE.get(tag, SnmpType.UNKNOWN)
    if kind is SnmpType.OCTET_STRING:
        return SnmpVarBind(oid, kind, raw=value if isinstance(value, bytes) else b"")
    if kind in (SnmpType.INTEGER, SnmpType.COUNTER, SnmpType.GAUGE, SnmpType.TIMETICKS):
        return SnmpVarBind(oid, kind, number=value if isinstance(value, int) else None)
    if kind in (SnmpType.IP_ADDRESS, SnmpType.OID):
        return SnmpVarBind(oid, kind, text_value=value or "")
    return SnmpVarBind(oid, SnmpType.UNKNOWN)


def _index_suffix(oid: str, base_oid: str) -> str:
    """Return the index portion of oid after base_oid, e.g. '1.2.3'."""
    base = base_oid if base_oid.startswith(".") else "." + base_oid
    if not oid.startswith(base + "."):
        raise SnmpError(f"OID {oid} is not under base {base}")
    return oid[len(base) + 1 :]


def _under(oid: str, base: str) -> bool:
    return oid == base or oid.startswith(base + ".")


def walk(
    host: str,
    community: str,
    base_oid: str,
    version: str = "2c",
    timeout: int = 3,
    retries: int = 1,
    max_repetitions: int = 10,
) -> dict[str, SnmpVarBind]:
    """Walk base_oid on host via repeated GETBULK, return {index_suffix: vb}.

    max_repetitions is GETBULK's rows-per-response. Weak embedded agents can
    choke assembling large bulk responses; lowering it trades more
    round-trips for a better chance each one succeeds.
    """
    label = mibs.name_for(base_oid)
    logger.debug(
        "walk %s %s (max_repetitions=%d timeout=%ss retries=%d)",
        host, label, max_repetitions, timeout, retries,
    )
    start = time.monotonic()
    # Overall safety bound so a stuck table can't loop forever over many
    # GETBULK round-trips (matches the old subprocess-timeout budget).
    budget = timeout * (retries + 1) * 8 + 10
    base = base_oid if base_oid.startswith(".") else "." + base_oid
    result: dict[str, SnmpVarBind] = {}
    current = base_oid

    while True:
        if time.monotonic() - start > budget:
            raise SnmpError(f"snmp walk timed out talking to {host} {base_oid}")
        try:
            error_status, vbs = _snmpv2c.getbulk(
                host, community, current, 0, max_repetitions, timeout, retries
            )
        except (TimeoutError, OSError) as exc:
            elapsed = time.monotonic() - start
            logger.debug("walk %s %s FAILED after %.2fs: %s", host, label, elapsed, exc)
            raise SnmpError(f"snmp walk failed for {host} {base_oid}: {exc}") from exc

        if error_status or not vbs:
            break
        prev = current
        done = False
        for r_oid, tag, value in vbs:
            if tag in _snmpv2c.EXCEPTION_TAGS or not _under(r_oid, base):
                done = True  # left the subtree or hit end-of-MIB
                break
            result[_index_suffix(r_oid, base_oid)] = _varbind_from_raw(r_oid, tag, value)
            current = r_oid
        if done or current == prev:  # done, or the agent failed to advance
            break

    elapsed = time.monotonic() - start
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
    """SNMP GET one OID. Returns None if the object doesn't exist or the
    request fails at the protocol level (a transport failure raises)."""
    label = mibs.name_for(oid)
    logger.debug("get %s %s (timeout=%ss retries=%d)", host, label, timeout, retries)
    start = time.monotonic()
    try:
        error_status, vbs = _snmpv2c.get(host, community, [oid], timeout, retries)
    except (TimeoutError, OSError) as exc:
        elapsed = time.monotonic() - start
        logger.debug("get %s %s FAILED after %.2fs: %s", host, label, elapsed, exc)
        raise SnmpError(f"snmp get failed for {host} {oid}: {exc}") from exc

    elapsed = time.monotonic() - start
    if error_status or not vbs:
        logger.debug("get %s %s: no value (error-status=%s)", host, label, error_status)
        return None
    r_oid, tag, value = vbs[0]
    if tag in _snmpv2c.EXCEPTION_TAGS or tag == _snmpv2c.TAG_NULL:
        logger.debug("get %s %s: no such object/instance", host, label)
        return None
    vb = _varbind_from_raw(r_oid, tag, value)
    logger.debug("get %s %s OK in %.2fs: %s", host, label, elapsed, vb.as_text())
    return vb


def mac_from_octets(octets: list[str]) -> str:
    """Format decimal OID index octets ('0','10','92',...) as a MAC. Used
    for MACs that arrive as part of an OID index rather than as a value."""
    return ":".join(f"{int(o):02x}" for o in octets)
