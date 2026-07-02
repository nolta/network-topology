from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from .collect import collect_switch
from .model import build_topology
from .render import render_all
from .snmp import SnmpError


def main() -> int:
    parser = argparse.ArgumentParser(description="Map an SNMP/LLDP-discoverable network.")
    parser.add_argument(
        "-c", "--config", type=Path, default=Path("config.yaml"), help="Path to config.yaml"
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=Path("output"), help="Output directory"
    )
    args = parser.parse_args()

    if not args.config.exists():
        print(
            f"Config file {args.config} not found. Copy config.example.yaml to "
            f"{args.config} and fill in your SNMP community string.",
            file=sys.stderr,
        )
        return 1

    config = yaml.safe_load(args.config.read_text())
    version = str(config.get("snmp_version", "2c"))
    community = config["community"]
    timeout = int(config.get("timeout_seconds", 3))
    retries = int(config.get("retries", 1))
    switch_ips = config["switches"]

    switches = []
    for ip in switch_ips:
        print(f"Collecting SNMP data from {ip} ...", file=sys.stderr)
        try:
            data = collect_switch(ip, community, version, timeout, retries)
        except SnmpError as exc:
            print(f"  WARNING: failed to collect from {ip}: {exc}", file=sys.stderr)
            continue
        print(f"  -> {data.sys_name}: {len(data.lldp_remote)} LLDP neighbors, "
              f"{len(data.fdb)} FDB entries", file=sys.stderr)
        switches.append(data)

    if not switches:
        print("No switches responded, nothing to build.", file=sys.stderr)
        return 1

    topo = build_topology(switches)
    render_all(topo, args.output)

    print(
        f"\nDone. {len(topo.devices)} devices, {len(topo.links)} links.\n"
        f"  {args.output / 'devices.csv'}\n"
        f"  {args.output / 'links.csv'}\n"
        f"  {args.output / 'topology.dot'}\n"
        f"  {args.output / 'topology.png'}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
