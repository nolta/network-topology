"""Port health: flag ports whose error counters are rising or whose link
has dropped, by comparing this collection against the previous one.

The switches' counters are cumulative since boot, so a single snapshot
only says "this port has had N errors, ever". What matters on a marginal
link (e.g. a fiber run with too much attenuation) is whether errors are
*still increasing*, so each run is diffed against the cached data from
the run before it.

This can't see optical power: these switches don't expose SFP
diagnostics over SNMP. It catches a link once it starts corrupting
frames or flapping, which is the next-best early warning.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import mibs
from .collect import SwitchData

logger = logging.getLogger(__name__)

ERROR_FIELDS = ("in_errors", "fcs_errors", "symbol_errors", "alignment_errors", "out_errors")
_ERROR_LABELS = {
    "in_errors": "in",
    "fcs_errors": "FCS",
    "symbol_errors": "symbol",
    "alignment_errors": "alignment",
    "out_errors": "out",
}
_OPER_UP = 1
_FIBER_MEDIA = {mibs.MAU_TYPE_NAMES[arc] for arc in mibs.MAU_FIBER_TYPES}
_COUNTER32_WRAP = 2**32
# Allowance for clock skew / collection duration when deciding whether a
# switch rebooted between runs, or whether a link change was just the
# ports coming up after that reboot.
_REBOOT_SLACK = timedelta(minutes=2)
_BOOT_LINK_SETTLE = timedelta(minutes=5)
# With no previous run to compare against, a link that came up this
# recently is still worth mentioning.
_RECENT_LINK_CHANGE = timedelta(hours=24)


@dataclass
class PortHealth:
    switch_ip: str
    switch_name: str
    port: str
    peer: str
    media: str
    fiber: bool
    speed_mbps: int | None
    up: bool
    link_since: datetime | None
    totals: dict
    deltas: dict | None  # None when there's no usable previous run
    flags: list = field(default_factory=list)


def _boot_time(at: datetime, uptime_ticks: int | None) -> datetime | None:
    if uptime_ticks is None:
        return None
    return at - timedelta(seconds=uptime_ticks / 100)


def _delta(cur: int | None, prev: int | None) -> int | None:
    if cur is None or prev is None:
        return None
    d = cur - prev
    return d + _COUNTER32_WRAP if d < 0 else d


def _peer_map(topo) -> dict:
    """(switch_ip, port name) -> "peer label port" from the topology's
    links, so the report can say what's on the other end."""
    peers = {}
    for link in topo.links:
        for dev, port, other, other_port in (
            (link.device_a, link.port_a, link.device_b, link.port_b),
            (link.device_b, link.port_b, link.device_a, link.port_a),
        ):
            if dev.startswith("switch:"):
                label = topo.devices[other].label
                peers[(dev[len("switch:"):], port)] = f"{label} {other_port}".strip()
    return peers


def check_ports(
    switches: list[SwitchData],
    collected_at: datetime,
    previous: list[SwitchData] | None,
    previous_at: datetime | None,
    topo=None,
) -> list[PortHealth]:
    prev_by_ip = {sw.ip: sw for sw in previous or []}
    peers = _peer_map(topo) if topo is not None else {}
    results = []
    for sw in switches:
        boot = _boot_time(collected_at, sw.sys_uptime)
        prev_sw = prev_by_ip.get(sw.ip)
        comparable = prev_sw is not None and previous_at is not None and prev_sw.port_stats
        rebooted = False
        if comparable:
            prev_boot = _boot_time(previous_at, prev_sw.sys_uptime)
            rebooted = boot is not None and prev_boot is not None and boot > prev_boot + _REBOOT_SLACK

        for ifindex, stats in sw.port_stats.items():
            prev_stats = prev_sw.port_stats.get(ifindex) if comparable else None
            up = stats.get("oper_status") == _OPER_UP
            was_up = prev_stats is not None and prev_stats.get("oper_status") == _OPER_UP
            if not up and not was_up:
                continue  # empty port, nothing to say

            name = sw.ifindex_to_name.get(ifindex, ifindex)
            link_since = None
            last_change = stats.get("last_change")
            if up and boot is not None and last_change is not None:
                link_since = boot + timedelta(seconds=last_change / 100)

            totals = {f: stats.get(f) for f in ERROR_FIELDS}
            deltas = None
            if rebooted:
                # Counters reset at boot, which was after the previous run,
                # so everything counted now is new.
                deltas = dict(totals)
            elif prev_stats is not None:
                deltas = {f: _delta(stats.get(f), prev_stats.get(f)) for f in ERROR_FIELDS}

            media = stats.get("media") or ""
            ph = PortHealth(
                switch_ip=sw.ip,
                switch_name=sw.sys_name,
                port=name,
                peer=peers.get((sw.ip, name), ""),
                media=media,
                fiber=media in _FIBER_MEDIA,
                speed_mbps=stats.get("speed_mbps"),
                up=up,
                link_since=link_since,
                totals=totals,
                deltas=deltas,
            )

            if deltas:
                rising = [f"{_ERROR_LABELS[f]} +{d}" for f, d in deltas.items() if d]
                if rising:
                    ph.flags.append("errors increasing: " + ", ".join(rising))
            if was_up and not up:
                ph.flags.append("link went down")
            elif up and link_since is not None:
                after_boot = boot is not None and link_since - boot < _BOOT_LINK_SETTLE
                if comparable and link_since > previous_at and not (rebooted and after_boot):
                    ph.flags.append(f"link changed at {link_since:%Y-%m-%d %H:%M}")
                elif not comparable and not after_boot and collected_at - link_since < _RECENT_LINK_CHANGE:
                    ph.flags.append(f"link came up recently ({link_since:%Y-%m-%d %H:%M})")
            if rebooted and ph.flags:
                ph.flags.append("switch rebooted since last run")
            results.append(ph)
    return results


def write_port_health_csv(results: list[PortHealth], path: Path) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["switch", "switch_name", "port", "peer", "media", "fiber", "speed_mbps", "status",
             "link_up_since"]
            + [f"total_{f}" for f in ERROR_FIELDS]
            + [f"new_{f}" for f in ERROR_FIELDS]
            + ["flags"]
        )
        for r in results:
            writer.writerow(
                [r.switch_ip, r.switch_name, r.port, r.peer, r.media, "yes" if r.fiber else "",
                 r.speed_mbps, "up" if r.up else "down",
                 r.link_since.isoformat(timespec="seconds") if r.link_since else ""]
                + [r.totals.get(f) for f in ERROR_FIELDS]
                + [("" if r.deltas is None else r.deltas.get(f)) for f in ERROR_FIELDS]
                + ["; ".join(r.flags)]
            )


def log_summary(results: list[PortHealth], collected_at: datetime, previous_at: datetime | None) -> None:
    flagged = [r for r in results if r.flags]
    if previous_at is None:
        logger.info(
            "\nPort health: no previous collection to compare against; error totals recorded "
            "for %d ports. The next run will show which counters are increasing.", len(results),
        )
    else:
        hours = (collected_at - previous_at).total_seconds() / 3600
        logger.info(
            "\nPort health: %d ports compared with the collection from %s (%.1f h ago).",
            len(results), f"{previous_at:%Y-%m-%d %H:%M}", hours,
        )
    if not flagged:
        logger.info("  No ports flagged.")
        return
    for r in flagged:
        what = f"{r.switch_name} ({r.switch_ip}) {r.port}"
        if r.fiber:
            what += " [fiber]"
        if r.peer:
            what += f" <-> {r.peer}"
        logger.warning("  %s: %s", what, "; ".join(r.flags))
