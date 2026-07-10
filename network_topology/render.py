"""Write out devices.csv, links.csv, topology.dot, and topology.png."""
from __future__ import annotations

import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx
from matplotlib.lines import Line2D

from .model import Topology

_KIND_COLOR = {
    "switch": "#8ecae6",
    "lldp-endpoint": "#95d5b2",
    "host": "#ffffff",
    "hub": "#ffb703",
}
_KIND_SHAPE = {  # networkx doesn't do per-node shapes easily; used for dot only
    "switch": "box",
    "lldp-endpoint": "ellipse",
    "host": "ellipse",
    "hub": "diamond",
}
# Human-readable legend labels for each node kind (what the color means).
_KIND_LABEL = {
    "switch": "Switch",
    "lldp-endpoint": "LLDP-identified device (named)",
    "host": "Host: single MAC, via FDB/ARP (unnamed)",
    "hub": "Unidentified: multiple MACs behind one port",
}
# Legend labels and draw styles for each edge kind: (label, color, linestyle, width).
_EDGE_LEGEND = {
    "lldp": ("LLDP link (confirmed port-to-port)", "#333333", "solid", 1.6),
    "inferred-switch": ("Inferred switch uplink (FDB correlation)", "#fb8500", "solid", 2.8),
    "inferred": ("Inferred host/hub attachment (FDB/ARP)", "#999999", "dashed", 1.0),
}


def write_devices_csv(topo: Topology, path: Path) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "label", "kind", "ip", "mac", "notes"])
        for dev in sorted(topo.devices.values(), key=lambda d: (d.kind, d.label)):
            writer.writerow([dev.id, dev.label, dev.kind, dev.ip, dev.mac, dev.notes])


def write_links_csv(topo: Topology, path: Path) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["device_a", "label_a", "port_a", "device_b", "label_b", "port_b", "link_type"]
        )
        for link in topo.links:
            label_a = topo.devices[link.device_a].label
            label_b = topo.devices[link.device_b].label
            writer.writerow(
                [
                    link.device_a,
                    label_a,
                    link.port_a,
                    link.device_b,
                    label_b,
                    link.port_b,
                    link.kind,
                ]
            )


def write_dot(topo: Topology, path: Path) -> None:
    lines = ["graph topology {", '  layout="dot";', "  overlap=false;", "  node [style=filled];"]
    for dev in topo.devices.values():
        color = _KIND_COLOR.get(dev.kind, "#ffffff")
        shape = _KIND_SHAPE.get(dev.kind, "ellipse")
        safe_label = dev.label.replace('"', "'")
        lines.append(
            f'  "{dev.id}" [label="{safe_label}", shape={shape}, fillcolor="{color}"];'
        )
    for link in topo.links:
        edge_label = f"{link.port_a} - {link.port_b}".strip(" -")
        if link.kind == "lldp":
            style, color = "solid", "#333333"
        elif link.kind == "inferred-switch":
            style, color = "solid", "#fb8500"
        else:
            style, color = "dashed", "#999999"
        lines.append(
            f'  "{link.device_a}" -- "{link.device_b}" '
            f'[label="{edge_label}", style={style}, color="{color}"];'
        )
    lines.append("}")
    path.write_text("\n".join(lines) + "\n")


def _clustered_layout(g: nx.Graph) -> dict:
    """Switches on an inner ring; each switch's own leaf devices fanned out
    in a wedge around it on an outer ring (alternating radius to reduce
    label overlap), instead of one global force layout for every node."""
    import math

    switch_ids = sorted(n for n, d in g.nodes(data=True) if d.get("kind") == "switch")
    n_switches = max(len(switch_ids), 1)
    switch_radius = 4.0
    leaf_radius = switch_radius + 2.2
    leaf_radius_jitter = 1.0

    pos = {}
    switch_angle = {}
    for i, sid in enumerate(switch_ids):
        angle = 2 * math.pi * i / n_switches
        switch_angle[sid] = angle
        pos[sid] = (switch_radius * math.cos(angle), switch_radius * math.sin(angle))

    leaves_by_switch: dict[str, list[str]] = {sid: [] for sid in switch_ids}
    members_by_hub: dict[str, list[str]] = {}
    orphans = []
    for n, d in g.nodes(data=True):
        if d.get("kind") == "switch":
            continue
        if d.get("kind") == "behind-hub":
            members_by_hub.setdefault(d.get("member_of"), []).append(n)
            continue
        home = next((nb for nb in g.neighbors(n) if nb in switch_angle), None)
        if home:
            leaves_by_switch[home].append(n)
        else:
            orphans.append(n)

    wedge = (2 * math.pi / n_switches) * 0.85
    for sid, leaves in leaves_by_switch.items():
        base_angle = switch_angle[sid]
        count = len(leaves)
        for i, leaf in enumerate(sorted(leaves)):
            offset = 0.0 if count == 1 else (i / (count - 1) - 0.5) * wedge
            angle = base_angle + offset
            radius = leaf_radius + (leaf_radius_jitter if i % 2 else 0.0)
            pos[leaf] = (radius * math.cos(angle), radius * math.sin(angle))

    outer_radius = leaf_radius + 2 * leaf_radius_jitter
    for i, n in enumerate(sorted(orphans)):
        angle = 2 * math.pi * i / max(len(orphans), 1)
        pos[n] = (outer_radius * math.cos(angle), outer_radius * math.sin(angle))

    # Fan each hub's member devices out just beyond the hub, on the far side
    # from its switch, so it reads as "one uplink, several devices behind it".
    for hub_id, members in members_by_hub.items():
        if hub_id not in pos:
            continue
        hx, hy = pos[hub_id]
        home_sw = next((nb for nb in g.neighbors(hub_id) if nb in switch_angle), None)
        sx, sy = pos[home_sw] if home_sw else (0.0, 0.0)
        dx, dy = hx - sx, hy - sy
        norm = math.hypot(dx, dy) or 1.0
        ux, uy = dx / norm, dy / norm  # outward unit vector (switch -> hub)
        px, py = -uy, ux               # perpendicular, for spreading the fan
        count = len(members)
        step, spread = 1.2, 1.5
        for i, m in enumerate(sorted(members)):
            t = 0.0 if count == 1 else (i / (count - 1) - 0.5)
            pos[m] = (hx + ux * step + px * t * spread, hy + uy * step + py * t * spread)

    return pos


def write_png(topo: Topology, path: Path) -> None:
    g = nx.Graph()
    for dev in topo.devices.values():
        attrs = {k: v for k, v in dev.__dict__.items() if k != "members"}
        g.add_node(dev.id, **attrs)
    for link in topo.links:
        g.add_edge(
            link.device_a,
            link.device_b,
            label=f"{link.port_a} - {link.port_b}".strip(" -"),
            kind=link.kind,
            is_backbone=(
                topo.devices[link.device_a].kind == "switch"
                and topo.devices[link.device_b].kind == "switch"
            ),
        )

    # Expand each hub pseudo-node: fan its member devices out behind it so
    # the single uplink to the switch is visually explicit. Diagram-only --
    # devices.csv keeps the hub as one row.
    member_edges = []
    for dev in topo.devices.values():
        if dev.kind != "hub" or not dev.members:
            continue
        for mac, ip in dev.members:
            mid = f"{dev.id}::{mac}"
            g.add_node(mid, kind="behind-hub", label=(ip or mac), member_of=dev.id)
            g.add_edge(dev.id, mid, kind="member")
            member_edges.append((dev.id, mid))

    pos = _clustered_layout(g)

    plt.figure(figsize=(22, 22))

    present_node_kinds = []
    for kind, color in _KIND_COLOR.items():
        nodes = [n for n, d in g.nodes(data=True) if d.get("kind") == kind]
        if not nodes:
            continue
        present_node_kinds.append(kind)
        size = 2400 if kind == "switch" else 500
        nx.draw_networkx_nodes(
            g, pos, nodelist=nodes, node_color=color, node_size=size,
            edgecolors="#333333", linewidths=1.0,
        )

    # Member devices behind a hub: small, white (unnamed single MACs).
    member_nodes = [n for n, d in g.nodes(data=True) if d.get("kind") == "behind-hub"]
    if member_nodes:
        nx.draw_networkx_nodes(
            g, pos, nodelist=member_nodes, node_color="#ffffff", node_size=250,
            edgecolors="#999999", linewidths=0.8,
        )

    lldp_edges = [(u, v) for u, v, d in g.edges(data=True) if d.get("kind") == "lldp"]
    switch_edges = [(u, v) for u, v, d in g.edges(data=True) if d.get("kind") == "inferred-switch"]
    inferred_edges = [(u, v) for u, v, d in g.edges(data=True) if d.get("kind") == "inferred"]
    nx.draw_networkx_edges(g, pos, edgelist=lldp_edges, width=1.2, edge_color="#333333", alpha=0.8)
    nx.draw_networkx_edges(g, pos, edgelist=switch_edges, width=2.8, edge_color="#fb8500")
    nx.draw_networkx_edges(
        g, pos, edgelist=inferred_edges, width=0.8, edge_color="#999999", style="dashed", alpha=0.7
    )
    # hub -> member links: thin amber to visually group them under the hub.
    nx.draw_networkx_edges(g, pos, edgelist=member_edges, width=0.6, edge_color="#d9a441", alpha=0.7)
    present_edge_kinds = [
        k for k, e in (("lldp", lldp_edges), ("inferred-switch", switch_edges), ("inferred", inferred_edges))
        if e
    ]

    switch_labels = {n: d["label"] for n, d in g.nodes(data=True) if d.get("kind") == "switch"}
    leaf_labels = {
        n: d["label"] for n, d in g.nodes(data=True)
        if d.get("kind") not in ("switch", "behind-hub")
    }
    member_labels = {n: d["label"] for n, d in g.nodes(data=True) if d.get("kind") == "behind-hub"}
    nx.draw_networkx_labels(g, pos, labels=switch_labels, font_size=10, font_weight="bold")
    nx.draw_networkx_labels(g, pos, labels=leaf_labels, font_size=7)
    nx.draw_networkx_labels(g, pos, labels=member_labels, font_size=6)

    # Only label the switch-to-switch backbone -- labeling every leaf edge
    # is what made the old layout unreadable; per-host port info is still
    # in links.csv.
    backbone_edge_labels = {
        (u, v): d["label"] for u, v, d in g.edges(data=True) if d.get("is_backbone") and d.get("label")
    }
    nx.draw_networkx_edge_labels(
        g, pos, edge_labels=backbone_edge_labels, font_size=8, font_color="#8a4300"
    )

    _draw_legend(present_node_kinds, present_edge_kinds)

    plt.axis("off")
    plt.tight_layout()
    plt.savefig(path, dpi=150)
    plt.close()


def _draw_legend(present_node_kinds: list, present_edge_kinds: list) -> None:
    """Legend mapping node color -> what it means and edge style -> how the
    link was discovered. Only shows entries actually present in this
    diagram so unused categories don't clutter it."""
    handles = []
    for kind in present_node_kinds:
        handles.append(
            Line2D(
                [], [], marker="o", linestyle="none", markersize=13,
                markerfacecolor=_KIND_COLOR[kind], markeredgecolor="#333333",
                label=_KIND_LABEL[kind],
            )
        )
    for kind in present_edge_kinds:
        label, color, style, width = _EDGE_LEGEND[kind]
        handles.append(Line2D([], [], color=color, linestyle=style, linewidth=width, label=label))

    if not handles:
        return
    plt.legend(
        handles=handles, loc="upper left", fontsize=11, title="Legend",
        title_fontsize=12, frameon=True, framealpha=0.95, borderpad=1.0, labelspacing=0.8,
    )


def render_all(topo: Topology, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    write_devices_csv(topo, output_dir / "devices.csv")
    write_links_csv(topo, output_dir / "links.csv")
    write_dot(topo, output_dir / "topology.dot")
    write_png(topo, output_dir / "topology.png")
