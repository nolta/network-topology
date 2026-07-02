"""Pull everything we need from one switch over SNMP."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import mibs
from .snmp import get, hexstring_to_mac, normalize_mac, walk


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
) -> SwitchData:
    data = SwitchData(ip=host)

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

    if_name = walk(host, community, mibs.IF_NAME, version, timeout, retries)
    if_descr = walk(host, community, mibs.IF_DESCR, version, timeout, retries)
    for ifindex, vb in if_descr.items():
        data.ifindex_to_name[ifindex] = vb.value
    for ifindex, vb in if_name.items():
        if vb.value:
            data.ifindex_to_name[ifindex] = vb.value

    if_phys = walk(host, community, mibs.IF_PHYS_ADDRESS, version, timeout, retries)
    for ifindex, vb in if_phys.items():
        if vb.type == "Hex-STRING":
            data.ifindex_to_mac[ifindex] = hexstring_to_mac(vb.value)

    bridge_port_ifindex = walk(
        host, community, mibs.DOT1D_BASE_PORT_IF_INDEX, version, timeout, retries
    )
    for bport, vb in bridge_port_ifindex.items():
        data.bridge_port_to_ifindex[bport] = vb.value

    lldp_loc_id_subtype = walk(
        host, community, mibs.LLDP_LOC_PORT_ID_SUBTYPE, version, timeout, retries
    )
    lldp_loc_id = walk(host, community, mibs.LLDP_LOC_PORT_ID, version, timeout, retries)
    lldp_loc_desc = walk(host, community, mibs.LLDP_LOC_PORT_DESC, version, timeout, retries)
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

    rem_chassis_subtype = walk(
        host, community, mibs.LLDP_REM_CHASSIS_ID_SUBTYPE, version, timeout, retries
    )
    rem_chassis_id = walk(host, community, mibs.LLDP_REM_CHASSIS_ID, version, timeout, retries)
    rem_port_subtype = walk(
        host, community, mibs.LLDP_REM_PORT_ID_SUBTYPE, version, timeout, retries
    )
    rem_port_id = walk(host, community, mibs.LLDP_REM_PORT_ID, version, timeout, retries)
    rem_port_desc = walk(host, community, mibs.LLDP_REM_PORT_DESC, version, timeout, retries)
    rem_sys_name = walk(host, community, mibs.LLDP_REM_SYS_NAME, version, timeout, retries)
    rem_sys_desc = walk(host, community, mibs.LLDP_REM_SYS_DESC, version, timeout, retries)

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

    fdb_port = walk(host, community, mibs.DOT1D_TP_FDB_PORT, version, timeout, retries)
    fdb_status = walk(host, community, mibs.DOT1D_TP_FDB_STATUS, version, timeout, retries)
    for index_suffix, port_vb in fdb_port.items():
        status_vb = fdb_status.get(index_suffix)
        if status_vb and status_vb.value != "3":  # 3 = learned/forwarding
            continue
        if port_vb.value == "0":
            continue
        mac = _mac_from_fdb_index(index_suffix)
        data.fdb.append((mac, port_vb.value))

    arp = walk(host, community, mibs.IP_NET_TO_MEDIA_PHYS_ADDRESS, version, timeout, retries)
    for index_suffix, vb in arp.items():
        if vb.type != "Hex-STRING":
            continue
        _, ip = _ip_from_arp_index(index_suffix)
        data.arp[hexstring_to_mac(vb.value)] = ip

    return data
