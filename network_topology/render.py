"""Write out devices.csv, links.csv, topology.dot, and topology.png."""
from __future__ import annotations

import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx

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
    orphans = []
    for n, d in g.nodes(data=True):
        if d.get("kind") == "switch":
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

    return pos


def write_png(topo: Topology, path: Path) -> None:
    g = nx.Graph()
    for dev in topo.devices.values():
        g.add_node(dev.id, **dev.__dict__)
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

    pos = _clustered_layout(g)

    plt.figure(figsize=(22, 22))

    for kind, color in _KIND_COLOR.items():
        nodes = [n for n, d in g.nodes(data=True) if d.get("kind") == kind]
        if not nodes:
            continue
        size = 2400 if kind == "switch" else 500
        nx.draw_networkx_nodes(
            g, pos, nodelist=nodes, node_color=color, node_size=size,
            edgecolors="#333333", linewidths=1.0,
        )

    lldp_edges = [(u, v) for u, v, d in g.edges(data=True) if d.get("kind") == "lldp"]
    switch_edges = [(u, v) for u, v, d in g.edges(data=True) if d.get("kind") == "inferred-switch"]
    inferred_edges = [(u, v) for u, v, d in g.edges(data=True) if d.get("kind") == "inferred"]
    nx.draw_networkx_edges(g, pos, edgelist=lldp_edges, width=1.2, edge_color="#333333", alpha=0.8)
    nx.draw_networkx_edges(g, pos, edgelist=switch_edges, width=2.8, edge_color="#fb8500")
    nx.draw_networkx_edges(
        g, pos, edgelist=inferred_edges, width=0.8, edge_color="#999999", style="dashed", alpha=0.7
    )

    switch_labels = {n: d["label"] for n, d in g.nodes(data=True) if d.get("kind") == "switch"}
    leaf_labels = {n: d["label"] for n, d in g.nodes(data=True) if d.get("kind") != "switch"}
    nx.draw_networkx_labels(g, pos, labels=switch_labels, font_size=10, font_weight="bold")
    nx.draw_networkx_labels(g, pos, labels=leaf_labels, font_size=7)

    # Only label the switch-to-switch backbone -- labeling every leaf edge
    # is what made the old layout unreadable; per-host port info is still
    # in links.csv.
    backbone_edge_labels = {
        (u, v): d["label"] for u, v, d in g.edges(data=True) if d.get("is_backbone") and d.get("label")
    }
    nx.draw_networkx_edge_labels(
        g, pos, edge_labels=backbone_edge_labels, font_size=8, font_color="#8a4300"
    )

    plt.axis("off")
    plt.tight_layout()
    plt.savefig(path, dpi=150)
    plt.close()


def render_all(topo: Topology, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    write_devices_csv(topo, output_dir / "devices.csv")
    write_links_csv(topo, output_dir / "links.csv")
    write_dot(topo, output_dir / "topology.dot")
    write_png(topo, output_dir / "topology.png")
