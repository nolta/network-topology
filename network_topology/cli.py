from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import yaml

from .collect import collect_switch
from .model import build_topology
from .render import render_all
from .snmp import SnmpError

logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="Map an SNMP/LLDP-discoverable network.")
    parser.add_argument(
        "-c", "--config", type=Path, default=Path("config.yaml"), help="Path to config.yaml"
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=Path("output"), help="Output directory"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Log every individual SNMP request (host, table, timing, success/failure) "
             "to help pinpoint exactly where and why a switch's collection is failing.",
    )
    args = parser.parse_args()

    # Root stays quiet so third-party libraries (matplotlib, etc.) don't
    # flood stderr with their own debug chatter -- -v only affects our
    # own package's loggers ("network_topology.*"), given their own
    # handler+formatter rather than propagating to root's plain one.
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    pkg_logger = logging.getLogger("network_topology")
    pkg_logger.propagate = False
    pkg_logger.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s" if args.verbose else "%(message)s",
            datefmt="%H:%M:%S",
        )
    )
    pkg_logger.addHandler(handler)

    if not args.config.exists():
        logger.error(
            "Config file %s not found. Copy config.example.yaml to %s and fill in "
            "your SNMP community string.",
            args.config, args.config,
        )
        return 1

    config = yaml.safe_load(args.config.read_text())
    version = str(config.get("snmp_version", "2c"))
    community = config["community"]
    timeout = int(config.get("timeout_seconds", 3))
    retries = int(config.get("retries", 1))
    max_repetitions = int(config.get("max_repetitions", 10))
    pace_seconds = float(config.get("pace_seconds", 0.0))
    switch_retries = int(config.get("switch_retries", 1))
    switch_ips = config["switches"]

    switches = []
    for ip in switch_ips:
        logger.info("Collecting SNMP data from %s ...", ip)
        data = None
        last_exc = None
        for attempt in range(1, switch_retries + 1):
            try:
                data = collect_switch(
                    ip, community, version, timeout, retries, max_repetitions, pace_seconds
                )
                break
            except SnmpError as exc:
                last_exc = exc
                if attempt < switch_retries:
                    logger.info(
                        "  attempt %d/%d failed (%s), retrying...", attempt, switch_retries, exc
                    )
                    time.sleep(2)
        if data is None:
            logger.warning("  WARNING: failed to collect from %s: %s", ip, last_exc)
            continue
        logger.info(
            "  -> %s: %d LLDP neighbors, %d FDB entries",
            data.sys_name, len(data.lldp_remote), len(data.fdb),
        )
        switches.append(data)

    if not switches:
        logger.error("No switches responded, nothing to build.")
        return 1

    topo = build_topology(switches)
    render_all(topo, args.output)

    logger.info(
        "\nDone. %d devices, %d links.\n"
        "  %s\n  %s\n  %s\n  %s",
        len(topo.devices), len(topo.links),
        args.output / "devices.csv", args.output / "links.csv",
        args.output / "topology.dot", args.output / "topology.png",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
