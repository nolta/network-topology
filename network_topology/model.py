"""Turn per-switch SNMP data into a device/link graph.

Link discovery, in priority order:
  1. LLDP (authoritative -- exact port on both ends).
  2. Bridge-MIB FDB + ARP, for ports LLDP didn't cover (dumb hosts, PCs,
     unmanaged switches).
     - A port with exactly one learned MAC is a confirmed direct host --
       its location becomes ground truth ("MAC X lives at switch A").
     - A port with several learned MACs and no LLDP data is ambiguous on
       its own. But on a flat, switched (non-routed) LAN, broadcast/ARP
       traffic means *every* switch eventually learns *every* active MAC
       somewhere, so simply matching "which switch has ever seen this MAC"
       doesn't discriminate -- everyone has seen everyone by then.
       What does discriminate is *where a MAC has a confirmed single-MAC
       leaf port*. So for each ambiguous port we vote: of the MACs behind
       it, which other switch has most of them as confirmed direct hosts?
       A clear majority vote (below) upgrades the port to an inferred
       switch-to-switch uplink; anything less stays an "unidentified
       device" with the vote breakdown recorded as a hint for manual
       tracing.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from .collect import SwitchData

# An ambiguous port is called a confident inferred switch-to-switch uplink
# only if the leading candidate switch accounts for at least this fraction
# of the port's MACs, has at least this many confirmed matches, and beats
# the runner-up by at least this ratio.
_UPLINK_MIN_RATIO = 0.5
_UPLINK_MIN_VOTES = 2
_UPLINK_MIN_MARGIN = 1.5


@dataclass
class Device:
    id: str
    label: str
    kind: str  # "switch" | "lldp-endpoint" | "host" | "hub"
    ip: str = ""
    mac: str = ""
    notes: str = ""


@dataclass
class Link:
    device_a: str
    port_a: str
    device_b: str
    port_b: str
    kind: str  # "lldp" | "inferred" | "inferred-switch"


@dataclass
class Topology:
    devices: dict = field(default_factory=dict)  # id -> Device
    links: list = field(default_factory=list)  # list[Link]

    def add_device(self, device: Device) -> None:
        if device.id not in self.devices:
            self.devices[device.id] = device

    def add_link(self, link: Link) -> None:
        key_a = (link.device_a, link.port_a)
        key_b = (link.device_b, link.port_b)
        canonical = frozenset((key_a, key_b))
        for existing in self.links:
            existing_key = frozenset(
                ((existing.device_a, existing.port_a), (existing.device_b, existing.port_b))
            )
            if existing_key == canonical:
                return  # already have this link (other side reported it too)
        self.links.append(link)


def _switch_id(ip: str) -> str:
    return f"switch:{ip}"


def _local_port_label(switch: SwitchData, local_port_num: str) -> str:
    entry = switch.lldp_local_ports.get(local_port_num)
    if entry and entry["desc"]:
        return entry["desc"]
    if entry and entry["id"]:
        return entry["id"]
    return f"port#{local_port_num}"


def _build_lookup_tables(switches: list[SwitchData]):
    mac_to_switch_ip = {}
    for sw in switches:
        # Interface MACs first (some vendors populate these); lldpLocChassisId
        # last since it's what neighbors actually report us as, so it wins
        # any collision and is the most reliable match.
        for mac in sw.ifindex_to_mac.values():
            mac_to_switch_ip[mac] = sw.ip
    for sw in switches:
        if sw.chassis_id:
            mac_to_switch_ip[sw.chassis_id] = sw.ip

    # sys_name is only useful as a fallback when it's unique -- switches
    # left at factory-default hostnames (e.g. two "IGS-6325-24P4X") would
    # otherwise cause a neighbor to resolve to the wrong (or even its own)
    # switch.
    sysname_counts: dict[str, int] = {}
    sysname_to_switch_ip = {}
    for sw in switches:
        if sw.sys_name:
            key = sw.sys_name.strip().lower()
            sysname_counts[key] = sysname_counts.get(key, 0) + 1
            sysname_to_switch_ip[key] = sw.ip
    for key, count in sysname_counts.items():
        if count > 1:
            del sysname_to_switch_ip[key]

    return mac_to_switch_ip, sysname_to_switch_ip


def _resolve_peer_switch(entry, mac_to_switch_ip, sysname_to_switch_ip) -> str | None:
    if entry.chassis_id and entry.chassis_id in mac_to_switch_ip:
        return mac_to_switch_ip[entry.chassis_id]
    if entry.sys_name and entry.sys_name.strip().lower() in sysname_to_switch_ip:
        return sysname_to_switch_ip[entry.sys_name.strip().lower()]
    return None


def _trailing_port_number(s: str) -> str | None:
    """Extract the trailing digit run from a port name, e.g. 'GE7' -> '7',
    'GigabitEthernet7' -> '7', 'GigabitEthernet 1/21' -> '21'. Used as a
    fallback join key when different tables name the same port
    differently (observed across Planet switch models)."""
    m = re.search(r"(\d+)\s*$", s.strip())
    return m.group(1) if m else None


def _lldp_covered_bridge_ports(switch: SwitchData) -> set[str]:
    """Bridge port numbers that already have an LLDP-derived link, so we
    don't also try to infer them from the FDB."""
    name_to_ifindex = {}
    trailing_num_to_ifindexes: dict[str, list[str]] = {}
    for ifindex, name in switch.ifindex_to_name.items():
        if not name:
            continue
        name_to_ifindex[name.strip().lower()] = ifindex
        num = _trailing_port_number(name)
        if num:
            trailing_num_to_ifindexes.setdefault(num, []).append(ifindex)
    ifindex_to_bridge_port = {v: k for k, v in switch.bridge_port_to_ifindex.items()}

    covered = set()
    remote_local_port_nums = {entry.local_port_num for entry in switch.lldp_remote}
    for port_num in remote_local_port_nums:
        local = switch.lldp_local_ports.get(port_num)
        if not local:
            continue
        candidates = [local.get("desc", ""), local.get("id", "")]

        ifindex = None
        for candidate in candidates:
            ifindex = name_to_ifindex.get(candidate.strip().lower())
            if ifindex:
                break
        if not ifindex:
            # Fall back to matching on trailing port number (handles e.g.
            # LLDP desc "GE7" vs IF-MIB name "GigabitEthernet7") -- only
            # when it's unambiguous (exactly one interface with that number).
            for candidate in candidates:
                num = _trailing_port_number(candidate)
                if num and len(trailing_num_to_ifindexes.get(num, [])) == 1:
                    ifindex = trailing_num_to_ifindexes[num][0]
                    break

        if ifindex and ifindex in ifindex_to_bridge_port:
            covered.add(ifindex_to_bridge_port[ifindex])
    return covered


def build_topology(switches: list[SwitchData]) -> Topology:
    topo = Topology()
    mac_to_switch_ip, sysname_to_switch_ip = _build_lookup_tables(switches)
    switches_by_ip = {sw.ip: sw for sw in switches}

    # Global ARP union: mac -> ip, seen from any switch.
    global_arp: dict[str, str] = {}
    for sw in switches:
        for mac, ip in sw.arp.items():
            global_arp.setdefault(mac, ip)

    all_switch_macs = set(mac_to_switch_ip.keys())

    for sw in switches:
        topo.add_device(
            Device(id=_switch_id(sw.ip), label=sw.sys_name or sw.ip, kind="switch", ip=sw.ip)
        )

    # --- Pass 1: LLDP-derived links ---
    for sw in switches:
        for entry in sw.lldp_remote:
            local_port_label = _local_port_label(sw, entry.local_port_num)
            peer_ip = _resolve_peer_switch(entry, mac_to_switch_ip, sysname_to_switch_ip)
            if peer_ip == sw.ip:
                peer_ip = None  # a switch can't be its own LLDP neighbor

            if peer_ip and peer_ip in switches_by_ip:
                topo.add_link(
                    Link(
                        device_a=_switch_id(sw.ip),
                        port_a=local_port_label,
                        device_b=_switch_id(peer_ip),
                        port_b=entry.port_desc or entry.port_id or "?",
                        kind="lldp",
                    )
                )
                continue

            # Not one of our known switches -- an LLDP-speaking endpoint
            # (AP, phone, printer, another unmanaged switch, etc).
            endpoint_id = f"lldp:{entry.chassis_id or entry.sys_name or entry.port_id}"
            label = entry.sys_name or entry.chassis_id or "unknown LLDP device"
            topo.add_device(
                Device(
                    id=endpoint_id,
                    label=label,
                    kind="lldp-endpoint",
                    mac=entry.chassis_id if entry.chassis_id_subtype == "4" else "",
                    notes=entry.sys_desc,
                )
            )
            topo.add_link(
                Link(
                    device_a=_switch_id(sw.ip),
                    port_a=local_port_label,
                    device_b=endpoint_id,
                    port_b=entry.port_id or entry.port_desc or "?",
                    kind="lldp",
                )
            )

    # --- Pass 2a: gather per-switch, per-port MAC sets (LLDP-covered ports excluded) ---
    per_switch_port_macs: dict[str, dict[str, list[str]]] = {}
    for sw in switches:
        covered_bridge_ports = _lldp_covered_bridge_ports(sw)
        macs_by_bridge_port: dict[str, list[str]] = {}
        for mac, bridge_port in sw.fdb:
            if bridge_port in covered_bridge_ports:
                continue
            if mac in all_switch_macs:
                continue  # switch management/uplink MAC leaking into FDB
            macs_by_bridge_port.setdefault(bridge_port, []).append(mac)
        per_switch_port_macs[sw.ip] = macs_by_bridge_port

    # --- Pass 2b: confirmed host locations, from ports with exactly one MAC ---
    mac_home: dict[str, str] = {}
    for sw in switches:
        for bridge_port, macs in per_switch_port_macs[sw.ip].items():
            if len(macs) == 1:
                mac_home[macs[0]] = sw.ip

    def _port_label(sw: SwitchData, bridge_port: str) -> str:
        ifindex = sw.bridge_port_to_ifindex.get(bridge_port)
        return sw.ifindex_to_name.get(ifindex, f"bridge-port#{bridge_port}")

    # --- Pass 2c: build links, upgrading confident ambiguous ports to switch links ---
    for sw in switches:
        for bridge_port, macs in per_switch_port_macs[sw.ip].items():
            port_label = _port_label(sw, bridge_port)

            if len(macs) == 1:
                mac = macs[0]
                ip = global_arp.get(mac, "")
                device_id = f"host:{mac}"
                topo.add_device(Device(id=device_id, label=ip or mac, kind="host", ip=ip, mac=mac))
                topo.add_link(
                    Link(
                        device_a=_switch_id(sw.ip),
                        port_a=port_label,
                        device_b=device_id,
                        port_b="",
                        kind="inferred",
                    )
                )
                continue

            votes = Counter(
                mac_home[mac] for mac in macs if mac in mac_home and mac_home[mac] != sw.ip
            )
            best_switch, best_count = (votes.most_common(1) or [(None, 0)])[0]
            runner_up_count = votes.most_common(2)[1][1] if len(votes) > 1 else 0
            confident = (
                best_switch is not None
                and best_switch in switches_by_ip
                and best_count >= _UPLINK_MIN_VOTES
                and best_count / len(macs) >= _UPLINK_MIN_RATIO
                and (runner_up_count == 0 or best_count >= _UPLINK_MIN_MARGIN * runner_up_count)
            )

            if confident:
                topo.add_link(
                    Link(
                        device_a=_switch_id(sw.ip),
                        port_a=port_label,
                        device_b=_switch_id(best_switch),
                        port_b="(uplink, inferred from FDB correlation)",
                        kind="inferred-switch",
                    )
                )
                continue

            vote_summary = ", ".join(
                f"{ip}: {count}/{len(macs)}" for ip, count in votes.most_common(3)
            )
            device_id = f"hub:{sw.ip}:{bridge_port}"
            mac_list = ", ".join(f"{m} ({global_arp.get(m, '?')})" for m in macs)
            notes = mac_list
            if vote_summary:
                notes = f"possible uplink toward: {vote_summary} | MACs: {mac_list}"
            topo.add_device(
                Device(
                    id=device_id,
                    label=f"unidentified device ({len(macs)} MACs behind port)",
                    kind="hub",
                    notes=notes,
                )
            )
            topo.add_link(
                Link(
                    device_a=_switch_id(sw.ip),
                    port_a=port_label,
                    device_b=device_id,
                    port_b="",
                    kind="inferred",
                )
            )

    _merge_reciprocal_switch_links(topo)
    return topo


def _merge_reciprocal_switch_links(topo: Topology) -> None:
    """If two switches each independently voted for the other (one
    inferred-switch link in each direction), collapse them into a single
    link carrying the real port label from both ends -- much better
    evidence than either one-sided guess alone."""
    switch_links = [l for l in topo.links if l.kind == "inferred-switch"]
    by_pair: dict[frozenset, list] = {}
    for link in switch_links:
        by_pair.setdefault(frozenset((link.device_a, link.device_b)), []).append(link)

    for pair, links in by_pair.items():
        if len(links) != 2:
            continue
        a, b = links
        if a.device_a != b.device_b or a.device_b != b.device_a:
            continue
        merged = Link(device_a=a.device_a, port_a=a.port_a, device_b=a.device_b, port_b=b.port_a, kind="inferred-switch")
        topo.links.remove(a)
        topo.links.remove(b)
        topo.links.append(merged)
