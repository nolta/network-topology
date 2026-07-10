"""Write out devices.csv, links.csv, topology.dot, and topology.png."""
from __future__ import annotations

import csv
import math
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


def _order_leaves(leaves: list, g: nx.Graph) -> list:
    """Order a switch's leaf nodes for the wedge fan so hub pseudo-nodes are
    spread evenly across it rather than clumped together. Node ids sort by
    kind (all "hub:*" land adjacent), which otherwise stacks the hubs -- and
    their member fans -- on nearly the same radial, making them look chained."""
    hubs = sorted(n for n in leaves if g.nodes[n].get("kind") == "hub")
    others = sorted(n for n in leaves if g.nodes[n].get("kind") != "hub")
    if not hubs or not others:
        return hubs + others
    total = len(others) + len(hubs)
    ordered = []
    oi = hi = 0
    for slot in range(total):
        # Place a hub once its evenly-spaced target fraction is reached
        # (a Bresenham-style interleave), otherwise the next plain leaf.
        if hi < len(hubs) and (hi + 0.5) / len(hubs) <= (slot + 0.5) / total:
            ordered.append(hubs[hi])
            hi += 1
        elif oi < len(others):
            ordered.append(others[oi])
            oi += 1
        else:
            ordered.append(hubs[hi])
            hi += 1
    return ordered


def _clustered_layout(g: nx.Graph) -> dict:
    """Switches on an inner ring; each switch's own leaf devices fanned out
    in a wedge around it on an outer ring, all at a single radius so no two
    neighbours ever sit on the same ray from the switch (which read as one
    being 'behind' the other). Each switch's angular span is proportional to
    how much it has attached, so busy switches get more room than sparse
    ones. Labels are rotated radially at draw time to stay legible without
    needing radius jitter."""
    switch_ids = sorted(n for n, d in g.nodes(data=True) if d.get("kind") == "switch")
    switch_set = set(switch_ids)
    n_switches = max(len(switch_ids), 1)
    switch_radius = 4.0
    leaf_radius = switch_radius + 2.6

    # Assign leaves/members to switches first (needs only graph membership,
    # not positions), so we can size each switch's span by its load.
    leaves_by_switch: dict[str, list[str]] = {sid: [] for sid in switch_ids}
    members_by_hub: dict[str, list[str]] = {}
    orphans = []
    for n, d in g.nodes(data=True):
        if d.get("kind") == "switch":
            continue
        if d.get("kind") == "behind-hub":
            members_by_hub.setdefault(d.get("member_of"), []).append(n)
            continue
        home = next((nb for nb in g.neighbors(n) if nb in switch_set), None)
        if home:
            leaves_by_switch[home].append(n)
        else:
            orphans.append(n)

    members_per_switch = {sid: 0 for sid in switch_ids}
    for hub_id, members in members_by_hub.items():
        home = next((nb for nb in g.neighbors(hub_id) if nb in switch_set), None)
        if home:
            members_per_switch[home] += len(members)

    # Angular span per switch: a floor (so the inner-ring nodes stay apart
    # and sparse switches still get a usable wedge) plus a share of the rest
    # proportional to load (direct leaves + half-weight for hub members,
    # which only occupy a small arc each).
    weights = {
        sid: len(leaves_by_switch[sid]) + 0.5 * members_per_switch[sid]
        for sid in switch_ids
    }
    total_w = sum(weights.values()) or 1.0
    min_span = min(2 * math.pi * 0.06, 2 * math.pi / n_switches)
    free = 2 * math.pi - n_switches * min_span

    pos = {}
    switch_angle = {}
    switch_span = {}
    cursor = 0.0
    for sid in switch_ids:
        span = min_span + free * weights[sid] / total_w
        center = cursor + span / 2
        switch_angle[sid] = center
        switch_span[sid] = span
        pos[sid] = (switch_radius * math.cos(center), switch_radius * math.sin(center))
        cursor += span

    # Spread a switch's leaves by a fixed per-leaf angular step (capped at
    # its wedge) rather than always filling the whole wedge. This keeps a
    # switch's devices clustered tightly around it -- closer to each other
    # than to other switches' devices -- instead of flinging a sparse
    # switch's few leaves out to the edges of its span.
    per_leaf_step = 0.09
    for sid in switch_ids:
        base_angle = switch_angle[sid]
        wedge = switch_span[sid] * 0.85
        ordered = _order_leaves(leaves_by_switch[sid], g)
        count = len(ordered)
        fan = min((count - 1) * per_leaf_step, wedge) if count > 1 else 0.0
        for i, leaf in enumerate(ordered):
            offset = 0.0 if count == 1 else (i / (count - 1) - 0.5) * fan
            angle = base_angle + offset
            pos[leaf] = (leaf_radius * math.cos(angle), leaf_radius * math.sin(angle))

    outer_radius = leaf_radius + 2.5
    for i, n in enumerate(sorted(orphans)):
        angle = 2 * math.pi * i / max(len(orphans), 1)
        pos[n] = (outer_radius * math.cos(angle), outer_radius * math.sin(angle))

    # Fan each hub's member devices out just beyond the hub, in a small arc
    # centered on the hub's own radial (origin-centered, like a mini switch
    # fan). Placing them on the same radial theme as everything else -- rather
    # than perpendicular to the skewed switch->hub line -- keeps them orderly
    # and their rotated labels pointing consistently outward.
    member_gap = 1.7    # how far members sit beyond their hub
    member_arc = 0.20   # angular spread of the member fan (radians)
    for hub_id, members in members_by_hub.items():
        if hub_id not in pos:
            continue
        hx, hy = pos[hub_id]
        hub_angle = math.atan2(hy, hx)
        member_r = math.hypot(hx, hy) + member_gap
        count = len(members)
        for i, m in enumerate(sorted(members)):
            t = 0.0 if count == 1 else (i / (count - 1) - 0.5)
            a = hub_angle + t * member_arc
            pos[m] = (member_r * math.cos(a), member_r * math.sin(a))

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
            kind=link.kind,
            is_backbone=(
                topo.devices[link.device_a].kind == "switch"
                and topo.devices[link.device_b].kind == "switch"
            ),
            # Keep which port belongs to which end, so backbone labels can be
            # placed at the correct switch rather than as an ambiguous
            # midpoint "A - B" string.
            end_a=link.device_a,
            port_a=link.port_a,
            port_b=link.port_b,
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

    # Switch labels stay centered on the node (bold). Leaf/member labels are
    # anchored just outside the node and rotated along the node's radial, so
    # angularly-close labels splay outward like spokes and don't overlap --
    # this is what lets us drop the radius jitter that caused the "stacked on
    # one ray" ambiguity.
    switch_labels = {n: d["label"] for n, d in g.nodes(data=True) if d.get("kind") == "switch"}
    nx.draw_networkx_labels(g, pos, labels=switch_labels, font_size=10, font_weight="bold")

    for n, d in g.nodes(data=True):
        kind = d.get("kind")
        if kind == "switch":
            continue
        x, y = pos[n]
        angle = math.atan2(y, x)
        r = math.hypot(x, y)
        deg = math.degrees(angle)
        if deg > 90 or deg < -90:  # keep text upright on the left half
            deg += 180
            ha = "right"
        else:
            ha = "left"
        label_r = r + 0.35
        plt.text(
            label_r * math.cos(angle), label_r * math.sin(angle), d["label"],
            rotation=deg, rotation_mode="anchor", ha=ha, va="center",
            fontsize=6 if kind == "behind-hub" else 7,
        )

    # Label the switch-to-switch backbone only (labeling every leaf edge made
    # the layout unreadable; per-host port info is still in links.csv). Each
    # port is drawn near ITS OWN switch end, not as a single midpoint "A - B"
    # string -- otherwise the reader can't tell which port belongs to which
    # switch (and it reads reversed depending on which side each switch fell).
    for u, v, d in g.edges(data=True):
        if not d.get("is_backbone"):
            continue
        u_is_a = u == d.get("end_a")
        port_u = d["port_a"] if u_is_a else d["port_b"]
        port_v = d["port_b"] if u_is_a else d["port_a"]
        xu, yu = pos[u]
        xv, yv = pos[v]
        deg = math.degrees(math.atan2(yv - yu, xv - xu))
        if deg > 90 or deg < -90:
            deg += 180
        for frac, port in ((0.18, port_u), (0.82, port_v)):
            if not port:
                continue
            plt.text(
                xu + (xv - xu) * frac, yu + (yv - yu) * frac, port,
                rotation=deg, rotation_mode="anchor", ha="center", va="center",
                fontsize=7, color="#8a4300",
                bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.85),
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
