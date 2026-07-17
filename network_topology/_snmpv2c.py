"""Minimal pure-Python SNMPv2c over UDP.

Just enough BER + GET/GETBULK to replace the net-snmp CLI tools, with no
external dependencies. SNMPv2c has no crypto (the "community" is sent in the
clear), so the whole protocol is a bit of BER encoding over a UDP socket.

This module deliberately knows nothing about SnmpVarBind/SnmpType -- it
returns raw ``(oid, ber_tag, value)`` tuples and lets snmp.py apply typing,
which keeps the layering one-directional (snmp.py -> _snmpv2c, never back).

References: RFC 3416 (SNMPv2 PDUs), X.690 (BER).
"""
from __future__ import annotations

import random
import socket

SNMP_PORT = 161

# Universal BER tags
TAG_INTEGER = 0x02
TAG_OCTET_STRING = 0x04
TAG_NULL = 0x05
TAG_OID = 0x06
TAG_SEQUENCE = 0x30  # constructed

# Application tags (SNMP)
TAG_IPADDRESS = 0x40
TAG_COUNTER32 = 0x41
TAG_GAUGE32 = 0x42
TAG_TIMETICKS = 0x43
TAG_OPAQUE = 0x44
TAG_COUNTER64 = 0x46

# Context-specific value tags used in responses to mark a missing value
TAG_NO_SUCH_OBJECT = 0x80
TAG_NO_SUCH_INSTANCE = 0x81
TAG_END_OF_MIB_VIEW = 0x82
EXCEPTION_TAGS = frozenset({TAG_NO_SUCH_OBJECT, TAG_NO_SUCH_INSTANCE, TAG_END_OF_MIB_VIEW})

# PDU tags (context-specific, constructed)
PDU_GET = 0xA0
PDU_GETNEXT = 0xA1
PDU_RESPONSE = 0xA2
PDU_GETBULK = 0xA5

VERSION_V2C = 1


class SnmpProtocolError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# BER encoding
# --------------------------------------------------------------------------

def _enc_length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    out = bytearray()
    while n:
        out.append(n & 0xFF)
        n >>= 8
    out.reverse()
    return bytes([0x80 | len(out)]) + bytes(out)


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _enc_length(len(value)) + value


def _enc_uint(n: int) -> bytes:
    """Minimal big-endian encoding of a non-negative integer, with a leading
    zero byte if needed so the value isn't misread as negative. (We only ever
    send non-negative integers: version, request-id, counts.)"""
    if n == 0:
        return b"\x00"
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    if b[0] & 0x80:
        b = b"\x00" + b
    return b


def _enc_base128(n: int) -> bytes:
    if n == 0:
        return b"\x00"
    out = bytearray()
    while n:
        out.append(n & 0x7F)
        n >>= 7
    out.reverse()
    for i in range(len(out) - 1):
        out[i] |= 0x80
    return bytes(out)


def _enc_oid(oid: str) -> bytes:
    arcs = [int(a) for a in oid.strip(".").split(".") if a != ""]
    if len(arcs) < 2:
        raise SnmpProtocolError(f"OID too short: {oid!r}")
    body = _enc_base128(40 * arcs[0] + arcs[1])
    for a in arcs[2:]:
        body += _enc_base128(a)
    return body


def _int_tlv(n: int) -> bytes:
    return _tlv(TAG_INTEGER, _enc_uint(n))


def _build_pdu(pdu_tag: int, request_id: int, oids: list[str],
               non_repeaters: int = 0, max_repetitions: int = 0) -> bytes:
    varbinds = b"".join(
        _tlv(TAG_SEQUENCE, _tlv(TAG_OID, _enc_oid(oid)) + _tlv(TAG_NULL, b""))
        for oid in oids
    )
    # For GETBULK the 2nd/3rd fields are non-repeaters/max-repetitions;
    # for GET/GETNEXT they are error-status/error-index (0 in a request).
    field2, field3 = (
        (non_repeaters, max_repetitions) if pdu_tag == PDU_GETBULK else (0, 0)
    )
    body = (
        _int_tlv(request_id)
        + _int_tlv(field2)
        + _int_tlv(field3)
        + _tlv(TAG_SEQUENCE, varbinds)
    )
    return _tlv(pdu_tag, body)


def _build_message(community: str, pdu: bytes) -> bytes:
    body = _int_tlv(VERSION_V2C) + _tlv(TAG_OCTET_STRING, community.encode()) + pdu
    return _tlv(TAG_SEQUENCE, body)


# --------------------------------------------------------------------------
# BER decoding
# --------------------------------------------------------------------------

def _read_tlv(data: bytes, i: int) -> tuple[int, bytes, int]:
    """Return (tag, value_bytes, next_index)."""
    if i + 2 > len(data):
        raise SnmpProtocolError("truncated TLV")
    tag = data[i]
    length = data[i + 1]
    i += 2
    if length & 0x80:
        nbytes = length & 0x7F
        length = int.from_bytes(data[i : i + nbytes], "big")
        i += nbytes
    value = data[i : i + length]
    if len(value) != length:
        raise SnmpProtocolError("truncated value")
    return tag, value, i + length


def _dec_int(b: bytes) -> int:
    return int.from_bytes(b, "big", signed=True) if b else 0


def _dec_uint(b: bytes) -> int:
    return int.from_bytes(b, "big") if b else 0


def _dec_oid(b: bytes) -> str:
    if not b:
        return ""
    subids = []
    val = 0
    for byte in b:
        val = (val << 7) | (byte & 0x7F)
        if not (byte & 0x80):
            subids.append(val)
            val = 0
    first = subids[0]
    arcs = ([first // 40, first % 40] if first < 80 else [2, first - 80]) + subids[1:]
    return "." + ".".join(str(a) for a in arcs)


def _decode_value(tag: int, b: bytes):
    """Decode a varbind value to a Python object: bytes for OCTET STRING,
    int for the integer-ish types, str for OID/IpAddress, None otherwise."""
    if tag in (TAG_OCTET_STRING, TAG_OPAQUE):
        return b
    if tag == TAG_INTEGER:
        return _dec_int(b)
    if tag in (TAG_COUNTER32, TAG_GAUGE32, TAG_TIMETICKS, TAG_COUNTER64):
        return _dec_uint(b)
    if tag == TAG_OID:
        return _dec_oid(b)
    if tag == TAG_IPADDRESS:
        return ".".join(str(x) for x in b)
    return None  # NULL / noSuchObject / noSuchInstance / endOfMibView


def _parse_response(data: bytes) -> tuple[int, int, list[tuple[str, int, object]]]:
    """Return (request_id, error_status, [(oid, tag, value), ...])."""
    tag, msg, _ = _read_tlv(data, 0)
    if tag != TAG_SEQUENCE:
        raise SnmpProtocolError("response is not a SEQUENCE")
    i = 0
    _, _version, i = _read_tlv(msg, i)
    _, _community, i = _read_tlv(msg, i)
    pdu_tag, pdu, _ = _read_tlv(msg, i)
    if pdu_tag != PDU_RESPONSE:
        raise SnmpProtocolError(f"unexpected PDU tag 0x{pdu_tag:02x}")

    j = 0
    _, reqid_b, j = _read_tlv(pdu, j)
    _, errstat_b, j = _read_tlv(pdu, j)
    _, _errindex_b, j = _read_tlv(pdu, j)
    _, vblist, _ = _read_tlv(pdu, j)

    varbinds: list[tuple[str, int, object]] = []
    k = 0
    while k < len(vblist):
        _, vb, k = _read_tlv(vblist, k)
        m = 0
        _, oid_b, m = _read_tlv(vb, m)
        val_tag, val_b, _ = _read_tlv(vb, m)
        varbinds.append((_dec_oid(oid_b), val_tag, _decode_value(val_tag, val_b)))
    return _dec_int(reqid_b), _dec_int(errstat_b), varbinds


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------

def _transact(host: str, community: str, pdu: bytes, request_id: int,
              timeout: float, retries: int) -> tuple[int, list[tuple[str, int, object]]]:
    """Send one PDU, return (error_status, varbinds). Retries on timeout.
    Raises TimeoutError if no matching response arrives, OSError on socket
    errors (bad host, network unreachable, ...)."""
    message = _build_message(community, pdu)
    last_error: Exception | None = None
    for _ in range(retries + 1):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            sock.sendto(message, (host, SNMP_PORT))
            while True:  # skip any stray/mismatched datagram until our reply or timeout
                data, _addr = sock.recvfrom(65535)
                try:
                    rid, errstat, vbs = _parse_response(data)
                except SnmpProtocolError:
                    continue
                if rid == request_id:
                    return errstat, vbs
        except socket.timeout as exc:
            last_error = exc
        finally:
            sock.close()
    raise TimeoutError(f"no SNMP response from {host}") from last_error


def get(host: str, community: str, oids: list[str], timeout: float, retries: int):
    """SNMP GET. Returns (error_status, [(oid, tag, value), ...])."""
    rid = random.randint(1, 0x7FFFFFFF)
    return _transact(host, community, _build_pdu(PDU_GET, rid, oids), rid, timeout, retries)


def getbulk(host: str, community: str, start_oid: str, non_repeaters: int,
            max_repetitions: int, timeout: float, retries: int):
    """SNMP GETBULK from start_oid. Returns (error_status, [(oid, tag, value), ...])."""
    rid = random.randint(1, 0x7FFFFFFF)
    pdu = _build_pdu(PDU_GETBULK, rid, [start_oid], non_repeaters, max_repetitions)
    return _transact(host, community, pdu, rid, timeout, retries)
