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
        )

    pos = nx.spring_layout(g, seed=42, k=1.4)

    plt.figure(figsize=(16, 12))

    for kind, color in _KIND_COLOR.items():
        nodes = [n for n, d in g.nodes(data=True) if d.get("kind") == kind]
        if not nodes:
            continue
        size = 1800 if kind == "switch" else 700
        nx.draw_networkx_nodes(
            g, pos, nodelist=nodes, node_color=color, node_size=size,
            edgecolors="#333333", linewidths=1.0,
        )

    lldp_edges = [(u, v) for u, v, d in g.edges(data=True) if d.get("kind") == "lldp"]
    switch_edges = [(u, v) for u, v, d in g.edges(data=True) if d.get("kind") == "inferred-switch"]
    inferred_edges = [(u, v) for u, v, d in g.edges(data=True) if d.get("kind") == "inferred"]
    nx.draw_networkx_edges(g, pos, edgelist=lldp_edges, width=1.6, edge_color="#333333")
    nx.draw_networkx_edges(g, pos, edgelist=switch_edges, width=2.2, edge_color="#fb8500")
    nx.draw_networkx_edges(
        g, pos, edgelist=inferred_edges, width=1.0, edge_color="#999999", style="dashed"
    )

    labels = {n: d["label"] for n, d in g.nodes(data=True)}
    nx.draw_networkx_labels(g, pos, labels=labels, font_size=8)

    edge_labels = {(u, v): d["label"] for u, v, d in g.edges(data=True) if d.get("label")}
    nx.draw_networkx_edge_labels(g, pos, edge_labels=edge_labels, font_size=6)

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
