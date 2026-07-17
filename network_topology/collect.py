"""Pull everything we need from one switch over SNMP."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import mibs
from .snmp import SnmpError, get, mac_from_octets, walk

logger = logging.getLogger(__name__)

# Bump when the SwitchData/LldpRemoteEntry shape changes so stale caches
# are rejected rather than silently mis-loaded.
_CACHE_VERSION = 1


@dataclass
class LldpRemoteEntry:
    local_port_num: str
    chassis_id_subtype: str
    chassis_id: str
    port_id_subtype: str
    port_id: str
    port_desc: str
    sys_name: str
    sys_desc: str


@dataclass
class SwitchData:
    ip: str
    sys_name: str = ""
    sys_descr: str = ""
    chassis_id: str = ""
    ifindex_to_name: dict = field(default_factory=dict)
    ifindex_to_mac: dict = field(default_factory=dict)
    bridge_port_to_ifindex: dict = field(default_factory=dict)
    lldp_local_ports: dict = field(default_factory=dict)  # port_num -> {id, id_subtype, desc}
    lldp_remote: list = field(default_factory=list)  # list[LldpRemoteEntry]
    fdb: list = field(default_factory=list)  # list[(mac, bridge_port)]
    arp: dict = field(default_factory=dict)  # mac -> ip


def save_switches(switches: list[SwitchData], path: Path) -> None:
    """Persist raw collected data to JSON so diagrams can be regenerated
    without re-running the (slow) SNMP collection. Caching the raw
    SwitchData -- not the derived topology -- means a re-plot also picks up
    any later changes to the model/merge or rendering logic."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": _CACHE_VERSION,
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "switches": [asdict(sw) for sw in switches],
    }
    path.write_text(json.dumps(payload, indent=2))


def load_switches(path: Path) -> tuple[list[SwitchData], str]:
    """Load switches saved by save_switches. Returns (switches,
    collected_at). Raises ValueError on an unknown cache version."""
    payload = json.loads(Path(path).read_text())
    version = payload.get("version")
    if version != _CACHE_VERSION:
        raise ValueError(
            f"cache {path} has version {version!r}, this build expects {_CACHE_VERSION}; "
            "re-run a full collection to refresh it"
        )
    switches = []
    for d in payload["switches"]:
        d = dict(d)
        d["lldp_remote"] = [LldpRemoteEntry(**e) for e in d.get("lldp_remote", [])]
        d["fdb"] = [tuple(pair) for pair in d.get("fdb", [])]  # JSON lists -> tuples
        switches.append(SwitchData(**d))
    return switches, payload.get("collected_at", "unknown")


def _decode_id(subtype: str, vb) -> str:
    """Decode an LLDP chassis/port ID varbind according to its subtype.

    The subtype is what tells us how to read the bytes, so we branch on it
    explicitly. as_mac() copes with both a proper 6-byte value and vendors
    (e.g. Planet) that put the MAC in as ASCII text 'A8-F7-E0-89-A0-65'.
    """
    if vb is None:
        return ""
    if subtype in (mibs.CHASSIS_ID_SUBTYPE_MAC_ADDRESS, mibs.PORT_ID_SUBTYPE_MAC_ADDRESS):
        return vb.as_mac()
    return vb.as_text()


def _mac_from_fdb_index(index_suffix: str) -> str:
    """dot1dTpFdbTable index = 6 MAC octets."""
    return mac_from_octets(index_suffix.split("."))


def _mac_from_q_fdb_index(index_suffix: str) -> str:
    """dot1qTpFdbTable index = dot1qFdbId(VLAN).<6 MAC octets>. Take the
    trailing 6 octets; the leading VLAN id is a single dotted component
    regardless of its magnitude."""
    return mac_from_octets(index_suffix.split(".")[-6:])


def _ip_from_arp_index(index_suffix: str) -> tuple[str, str]:
    """ipNetToMediaPhysAddress index = ifIndex.ip1.ip2.ip3.ip4"""
    parts = index_suffix.split(".")
    ifindex, ip_octets = parts[0], parts[1:5]
    return ifindex, ".".join(ip_octets)


def _collect_fdb(host, data, _walk, _walk_optional) -> None:
    """Populate data.fdb with (mac, bridge_port) pairs.

    Prefers the Q-BRIDGE-MIB per-VLAN table (dot1qTpFdbTable): it matches
    the switch's native hardware layout, so the agent serves it cheaply.
    The legacy BRIDGE-MIB dot1dTpFdbTable forces a software flatten+sort
    that can peg the management CPU (observed pegging 24-port Planet
    IGS-6325 CPUs to 100% and timing the walk out entirely, vs ~1s for the
    Q-BRIDGE table). We only fall back to dot1d if Q-BRIDGE returns
    nothing, i.e. the switch doesn't implement it.

    The Q-BRIDGE table has a VLAN dimension, so a MAC learned on a trunk
    can appear once per VLAN; we dedupe identical (mac, port) pairs so a
    port's learned-MAC count isn't inflated by that.
    """
    q_fdb_port = _walk_optional(mibs.DOT1Q_TP_FDB_PORT)
    if q_fdb_port:
        q_fdb_status = _walk_optional(mibs.DOT1Q_TP_FDB_STATUS)
        seen = set()
        for index_suffix, port_vb in q_fdb_port.items():
            status_vb = q_fdb_status.get(index_suffix)
            if status_vb and status_vb.as_text() != "3":  # 3 = learned/forwarding
                continue
            if port_vb.as_text() == "0":
                continue
            mac = _mac_from_q_fdb_index(index_suffix)
            pair = (mac, port_vb.as_text())
            if pair in seen:
                continue
            seen.add(pair)
            data.fdb.append(pair)
        return

    # Fallback: legacy BRIDGE-MIB (may be slow/CPU-heavy on some switches).
    logger.debug("%s: Q-BRIDGE FDB empty, falling back to legacy dot1dTpFdbTable", host)
    fdb_port = _walk(mibs.DOT1D_TP_FDB_PORT)
    fdb_status = _walk_optional(mibs.DOT1D_TP_FDB_STATUS)
    for index_suffix, port_vb in fdb_port.items():
        status_vb = fdb_status.get(index_suffix)
        if status_vb and status_vb.as_text() != "3":  # 3 = learned/forwarding
            continue
        if port_vb.as_text() == "0":
            continue
        mac = _mac_from_fdb_index(index_suffix)
        data.fdb.append((mac, port_vb.as_text()))


def collect_switch(
    host: str,
    community: str,
    version: str = "2c",
    timeout: int = 3,
    retries: int = 1,
    max_repetitions: int = 10,
    pace_seconds: float = 0.0,
) -> SwitchData:
    logger.debug(
        "collect_switch %s starting (timeout=%ds retries=%d max_repetitions=%d pace_seconds=%.2f)",
        host, timeout, retries, max_repetitions, pace_seconds,
    )
    collect_start = time.monotonic()
    data = SwitchData(ip=host)

    def _walk(oid: str):
        result = walk(host, community, oid, version, timeout, retries, max_repetitions)
        if pace_seconds:
            logger.debug("pacing %.2fs before next request to %s", pace_seconds, host)
            time.sleep(pace_seconds)
        return result

    def _walk_optional(oid: str):
        try:
            return _walk(oid)
        except SnmpError:
            return {}

    sys_name_vb = get(host, community, mibs.SYS_NAME, version, timeout, retries)
    data.sys_name = sys_name_vb.as_text() if sys_name_vb else host
    sys_descr_vb = get(host, community, mibs.SYS_DESCR, version, timeout, retries)
    data.sys_descr = sys_descr_vb.as_text() if sys_descr_vb else ""

    loc_chassis_subtype_vb = get(
        host, community, mibs.LLDP_LOC_CHASSIS_ID_SUBTYPE, version, timeout, retries
    )
    loc_chassis_id_vb = get(host, community, mibs.LLDP_LOC_CHASSIS_ID, version, timeout, retries)
    if loc_chassis_subtype_vb and loc_chassis_id_vb:
        data.chassis_id = _decode_id(loc_chassis_subtype_vb.as_text(), loc_chassis_id_vb)

    if_name = _walk(mibs.IF_NAME)
    if_descr = _walk(mibs.IF_DESCR)
    for ifindex, vb in if_descr.items():
        data.ifindex_to_name[ifindex] = vb.as_text()
    for ifindex, vb in if_name.items():
        if vb.as_text():
            data.ifindex_to_name[ifindex] = vb.as_text()

    if_phys = _walk(mibs.IF_PHYS_ADDRESS)
    for ifindex, vb in if_phys.items():
        # Only genuine 6-byte addresses; VLAN/loopback interfaces report an
        # empty ifPhysAddress.
        if vb.is_mac_bytes:
            data.ifindex_to_mac[ifindex] = vb.as_mac()

    bridge_port_ifindex = _walk(mibs.DOT1D_BASE_PORT_IF_INDEX)
    for bport, vb in bridge_port_ifindex.items():
        data.bridge_port_to_ifindex[bport] = vb.as_text()

    lldp_loc_id_subtype = _walk(mibs.LLDP_LOC_PORT_ID_SUBTYPE)
    lldp_loc_id = _walk(mibs.LLDP_LOC_PORT_ID)
    lldp_loc_desc = _walk(mibs.LLDP_LOC_PORT_DESC)
    # Union across all three columns: some vendors don't implement every
    # column (e.g. lldpLocPortIdSubtype missing entirely on some Planet
    # models), so keying off just one column can silently drop every port.
    local_port_nums = set(lldp_loc_id_subtype) | set(lldp_loc_id) | set(lldp_loc_desc)
    for port_num in local_port_nums:
        subtype_vb = lldp_loc_id_subtype.get(port_num)
        subtype = subtype_vb.as_text() if subtype_vb else ""
        id_vb = lldp_loc_id.get(port_num)
        desc_vb = lldp_loc_desc.get(port_num)
        data.lldp_local_ports[port_num] = {
            "id_subtype": subtype,
            "id": _decode_id(subtype, id_vb) if subtype else (id_vb.as_text() if id_vb else ""),
            "desc": desc_vb.as_text() if desc_vb else "",
        }

    rem_chassis_subtype = _walk(mibs.LLDP_REM_CHASSIS_ID_SUBTYPE)
    rem_chassis_id = _walk(mibs.LLDP_REM_CHASSIS_ID)
    rem_port_subtype = _walk(mibs.LLDP_REM_PORT_ID_SUBTYPE)
    rem_port_id = _walk(mibs.LLDP_REM_PORT_ID)
    rem_port_desc = _walk(mibs.LLDP_REM_PORT_DESC)
    rem_sys_name = _walk(mibs.LLDP_REM_SYS_NAME)
    rem_sys_desc = _walk(mibs.LLDP_REM_SYS_DESC)

    for index_suffix, chassis_subtype_vb in rem_chassis_subtype.items():
        # index_suffix = timeMark.localPortNum.remIndex
        parts = index_suffix.split(".")
        local_port_num = parts[1] if len(parts) >= 2 else index_suffix
        chassis_subtype = chassis_subtype_vb.as_text()
        port_subtype_vb = rem_port_subtype.get(index_suffix)
        port_subtype = port_subtype_vb.as_text() if port_subtype_vb else ""
        entry = LldpRemoteEntry(
            local_port_num=local_port_num,
            chassis_id_subtype=chassis_subtype,
            chassis_id=_decode_id(chassis_subtype, rem_chassis_id.get(index_suffix)),
            port_id_subtype=port_subtype,
            port_id=_decode_id(port_subtype, rem_port_id.get(index_suffix)),
            port_desc=(rem_port_desc[index_suffix].as_text() if index_suffix in rem_port_desc else ""),
            sys_name=(rem_sys_name[index_suffix].as_text() if index_suffix in rem_sys_name else ""),
            sys_desc=(rem_sys_desc[index_suffix].as_text() if index_suffix in rem_sys_desc else ""),
        )
        data.lldp_remote.append(entry)

    _collect_fdb(host, data, _walk, _walk_optional)

    arp = _walk(mibs.IP_NET_TO_MEDIA_PHYS_ADDRESS)
    for index_suffix, vb in arp.items():
        if not vb.is_mac_bytes:
            continue
        _, ip = _ip_from_arp_index(index_suffix)
        data.arp[vb.as_mac()] = ip

    logger.debug("collect_switch %s finished in %.2fs", host, time.monotonic() - collect_start)
    return data
