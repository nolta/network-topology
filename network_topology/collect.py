"""Pull everything we need from one switch over SNMP."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import mibs
from .snmp import SnmpError, get, hexstring_to_mac, normalize_mac, walk


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


def _decode_id(subtype: str, vb) -> str:
    """Decode an LLDP chassis/port ID varbind based on its subtype.

    Some vendors (e.g. Planet) encode MAC-address-subtype IDs as literal
    ASCII text ('A8-F7-E0-89-A0-65') rather than raw MAC bytes, so net-snmp
    tags them STRING instead of Hex-STRING. normalize_mac() handles both
    representations and leaves non-MAC values untouched.
    """
    if vb is None:
        return ""
    if subtype in (mibs.CHASSIS_ID_SUBTYPE_MAC_ADDRESS, mibs.PORT_ID_SUBTYPE_MAC_ADDRESS):
        return normalize_mac(vb.value)
    return vb.value


def _mac_from_fdb_index(index_suffix: str) -> str:
    octets = index_suffix.split(".")
    return ":".join(f"{int(o):02x}" for o in octets)


def _ip_from_arp_index(index_suffix: str) -> tuple[str, str]:
    """ipNetToMediaPhysAddress index = ifIndex.ip1.ip2.ip3.ip4"""
    parts = index_suffix.split(".")
    ifindex, ip_octets = parts[0], parts[1:5]
    return ifindex, ".".join(ip_octets)


def collect_switch(
    host: str,
    community: str,
    version: str = "2c",
    timeout: int = 3,
    retries: int = 1,
    max_repetitions: int = 10,
    pace_seconds: float = 0.0,
) -> SwitchData:
    data = SwitchData(ip=host)

    def _walk(oid: str):
        result = walk(host, community, oid, version, timeout, retries, max_repetitions)
        if pace_seconds:
            time.sleep(pace_seconds)
        return result

    def _walk_optional(oid: str):
        try:
            return _walk(oid)
        except SnmpError:
            return {}

    sys_name_vb = get(host, community, mibs.SYS_NAME, version, timeout, retries)
    data.sys_name = sys_name_vb.value if sys_name_vb else host
    sys_descr_vb = get(host, community, mibs.SYS_DESCR, version, timeout, retries)
    data.sys_descr = sys_descr_vb.value if sys_descr_vb else ""

    loc_chassis_subtype_vb = get(
        host, community, mibs.LLDP_LOC_CHASSIS_ID_SUBTYPE, version, timeout, retries
    )
    loc_chassis_id_vb = get(host, community, mibs.LLDP_LOC_CHASSIS_ID, version, timeout, retries)
    if loc_chassis_subtype_vb and loc_chassis_id_vb:
        data.chassis_id = _decode_id(loc_chassis_subtype_vb.value, loc_chassis_id_vb)

    if_name = _walk(mibs.IF_NAME)
    if_descr = _walk(mibs.IF_DESCR)
    for ifindex, vb in if_descr.items():
        data.ifindex_to_name[ifindex] = vb.value
    for ifindex, vb in if_name.items():
        if vb.value:
            data.ifindex_to_name[ifindex] = vb.value

    if_phys = _walk(mibs.IF_PHYS_ADDRESS)
    for ifindex, vb in if_phys.items():
        if vb.type == "Hex-STRING":
            data.ifindex_to_mac[ifindex] = hexstring_to_mac(vb.value)

    bridge_port_ifindex = _walk(mibs.DOT1D_BASE_PORT_IF_INDEX)
    for bport, vb in bridge_port_ifindex.items():
        data.bridge_port_to_ifindex[bport] = vb.value

    lldp_loc_id_subtype = _walk(mibs.LLDP_LOC_PORT_ID_SUBTYPE)
    lldp_loc_id = _walk(mibs.LLDP_LOC_PORT_ID)
    lldp_loc_desc = _walk(mibs.LLDP_LOC_PORT_DESC)
    # Union across all three columns: some vendors don't implement every
    # column (e.g. lldpLocPortIdSubtype missing entirely on some Planet
    # models), so keying off just one column can silently drop every port.
    local_port_nums = set(lldp_loc_id_subtype) | set(lldp_loc_id) | set(lldp_loc_desc)
    for port_num in local_port_nums:
        subtype_vb = lldp_loc_id_subtype.get(port_num)
        subtype = subtype_vb.value if subtype_vb else ""
        id_vb = lldp_loc_id.get(port_num)
        desc_vb = lldp_loc_desc.get(port_num)
        data.lldp_local_ports[port_num] = {
            "id_subtype": subtype,
            "id": _decode_id(subtype, id_vb) if subtype else (id_vb.value if id_vb else ""),
            "desc": desc_vb.value if desc_vb else "",
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
        chassis_subtype = chassis_subtype_vb.value
        port_subtype_vb = rem_port_subtype.get(index_suffix)
        port_subtype = port_subtype_vb.value if port_subtype_vb else ""
        entry = LldpRemoteEntry(
            local_port_num=local_port_num,
            chassis_id_subtype=chassis_subtype,
            chassis_id=_decode_id(chassis_subtype, rem_chassis_id.get(index_suffix)),
            port_id_subtype=port_subtype,
            port_id=_decode_id(port_subtype, rem_port_id.get(index_suffix)),
            port_desc=(rem_port_desc[index_suffix].value if index_suffix in rem_port_desc else ""),
            sys_name=(rem_sys_name[index_suffix].value if index_suffix in rem_sys_name else ""),
            sys_desc=(rem_sys_desc[index_suffix].value if index_suffix in rem_sys_desc else ""),
        )
        data.lldp_remote.append(entry)

    fdb_port = _walk(mibs.DOT1D_TP_FDB_PORT)
    # Optional: some switch models (e.g. the 24-port Planet IGS-6325-24P4X
    # in this network) reliably time out on this specific table. Fall back
    # to treating all learned FDB entries as valid rather than losing the
    # whole switch's data over one refinement column.
    fdb_status = _walk_optional(mibs.DOT1D_TP_FDB_STATUS)
    for index_suffix, port_vb in fdb_port.items():
        status_vb = fdb_status.get(index_suffix)
        if status_vb and status_vb.value != "3":  # 3 = learned/forwarding
            continue
        if port_vb.value == "0":
            continue
        mac = _mac_from_fdb_index(index_suffix)
        data.fdb.append((mac, port_vb.value))

    arp = _walk(mibs.IP_NET_TO_MEDIA_PHYS_ADDRESS)
    for index_suffix, vb in arp.items():
        if vb.type != "Hex-STRING":
            continue
        _, ip = _ip_from_arp_index(index_suffix)
        data.arp[hexstring_to_mac(vb.value)] = ip

    return data
