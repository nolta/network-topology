from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

from . import health
from .collect import SwitchData, collect_switch, load_switches, save_switches
from .model import build_topology
from .render import render_all
from .snmp import SnmpError

logger = logging.getLogger(__name__)


def _collect_with_retry(
    ip, community, version, timeout, retries, max_repetitions, pace_seconds, switch_retries,
    web_credentials=None,
) -> SwitchData | None:
    """Collect one switch, retrying the whole collection up to
    switch_retries times. Returns None (and logs a warning) if it never
    succeeds. Safe to run concurrently: it shares no mutable state and
    every log line names the host, so interleaved output stays traceable."""
    logger.info("Collecting SNMP data from %s ...", ip)
    last_exc = None
    for attempt in range(1, switch_retries + 1):
        try:
            data = collect_switch(
                ip, community, version, timeout, retries, max_repetitions, pace_seconds,
                web_credentials,
            )
            logger.info(
                "  -> %s (%s): %d LLDP neighbors, %d FDB entries",
                data.sys_name, ip, len(data.lldp_remote), len(data.fdb),
            )
            return data
        except SnmpError as exc:
            last_exc = exc
            if attempt < switch_retries:
                logger.info(
                    "  %s attempt %d/%d failed (%s), retrying...",
                    ip, attempt, switch_retries, exc,
                )
                time.sleep(2)
    logger.warning("  WARNING: failed to collect from %s: %s", ip, last_exc)
    return None


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
    parser.add_argument(
        "--replot", action="store_true",
        help="Skip SNMP collection and rebuild the diagram/CSVs from the cached data "
             "(<output>/collected.json) written by a previous run. Fast; no network access.",
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

    cache_path = args.output / "collected.json"
    port_health = None  # (switches, collected_at, previous, previous_at, thresholds) when collecting

    if args.replot:
        # Rebuild diagram/CSVs from previously collected data -- no network,
        # no config needed. Picks up any code changes to the model/render.
        if not cache_path.exists():
            logger.error(
                "--replot: no cached data at %s. Run a collection first (without --replot).",
                cache_path,
            )
            return 1
        try:
            switches, collected_at = load_switches(cache_path)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            logger.error("failed to load cached data %s: %s", cache_path, exc)
            return 1
        logger.info(
            "Re-plotting from cache %s (%d switches, collected %s) -- no SNMP collection.",
            cache_path, len(switches), collected_at,
        )
    else:
        if not args.config.exists():
            logger.error(
                "Config file %s not found. Copy config.example.yaml to %s and fill in "
                "your SNMP community string.",
                args.config, args.config,
            )
            return 1

        try:
            config = yaml.safe_load(args.config.read_text())
        except yaml.YAMLError as exc:
            # Report only the position: YAML's own message quotes the
            # offending line, which may be a password.
            # context_mark, when present, is where the broken construct
            # started (e.g. an unclosed quote), which is the useful place.
            mark = getattr(exc, "context_mark", None) or getattr(exc, "problem_mark", None)
            where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
            problem = " ".join(
                p for p in (getattr(exc, "context", None), getattr(exc, "problem", None)) if p
            ) or "syntax error"
            logger.error("Config file %s is not valid YAML%s: %s", args.config, where, problem)
            return 1
        version = str(config.get("snmp_version", "2c"))
        community = config["community"]
        timeout = int(config.get("timeout_seconds", 3))
        retries = int(config.get("retries", 1))
        max_repetitions = int(config.get("max_repetitions", 10))
        pace_seconds = float(config.get("pace_seconds", 0.0))
        switch_retries = int(config.get("switch_retries", 1))
        max_concurrency = int(config.get("max_concurrency", 8))
        switch_ips = config["switches"]
        # Optional web-UI login, used only to read SFP diagnostics.
        web_credentials = None
        if config.get("web_username") and config.get("web_password"):
            web_credentials = (str(config["web_username"]), str(config["web_password"]))
        else:
            logger.info("No web_username/web_password in config: skipping SFP diagnostics.")
        thresholds = health.Thresholds(
            sfp_rx_min_dbm=float(config.get("sfp_rx_min_dbm", health.Thresholds.sfp_rx_min_dbm)),
            sfp_rx_drop_db=float(config.get("sfp_rx_drop_db", health.Thresholds.sfp_rx_drop_db)),
            sfp_temp_max_c=float(config.get("sfp_temp_max_c", health.Thresholds.sfp_temp_max_c)),
        )

        # Each switch is collected independently and is I/O-bound (blocking on
        # UDP socket recv, which releases the GIL), so a thread pool collects
        # them concurrently. Results are gathered by IP and re-ordered to
        # match config order so output stays deterministic run-to-run.
        workers = max(1, min(max_concurrency, len(switch_ips)))
        logger.debug("collecting %d switches with up to %d concurrent workers", len(switch_ips), workers)
        args_for = lambda ip: (
            ip, community, version, timeout, retries, max_repetitions, pace_seconds, switch_retries,
            web_credentials,
        )
        results_by_ip: dict = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_collect_with_retry, *args_for(ip)): ip for ip in switch_ips}
            for fut in concurrent.futures.as_completed(futures):
                results_by_ip[futures[fut]] = fut.result()

        switches = [results_by_ip[ip] for ip in switch_ips if results_by_ip.get(ip) is not None]

        if not switches:
            logger.error("No switches responded, nothing to build.")
            return 1

        # The previous run's cache is the baseline for the port-health
        # check: counters are cumulative, so only the difference between
        # two runs shows what's still increasing. Read it before it's
        # overwritten below.
        previous, previous_at = _load_previous(cache_path)

        collected_at = save_switches(switches, cache_path)
        logger.debug("cached raw collected data to %s", cache_path)
        port_health = (switches, collected_at, previous, previous_at, thresholds)

    topo = build_topology(switches)
    render_all(topo, args.output)

    outputs = [
        args.output / "devices.csv", args.output / "links.csv",
        args.output / "topology.dot", args.output / "topology.png",
    ]
    # Skipped on --replot: there's no new snapshot to compare, and
    # rewriting port_health.csv from the cache would lose its deltas.
    if port_health is not None:
        current, collected_at, previous, previous_at, thresholds = port_health
        results = health.check_ports(current, collected_at, previous, previous_at, topo, thresholds)
        health.write_port_health_csv(results, args.output / "port_health.csv")
        outputs.append(args.output / "port_health.csv")
        health.log_summary(results, collected_at, previous_at)

    logger.info(
        "\nDone. %d devices, %d links.\n%s",
        len(topo.devices), len(topo.links), "\n".join(f"  {p}" for p in outputs),
    )
    return 0


def _load_previous(cache_path: Path) -> tuple[list[SwitchData] | None, datetime | None]:
    """The previous run's switches and collection time, or (None, None)
    if there's no usable baseline (no cache, unreadable, or from a build
    that didn't collect port stats)."""
    if not cache_path.exists():
        return None, None
    try:
        previous, previous_at = load_switches(cache_path)
        previous_at = datetime.fromisoformat(previous_at)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        logger.debug("no port-health baseline: can't load %s: %s", cache_path, exc)
        return None, None
    if not any(sw.port_stats for sw in previous):
        return None, None
    return previous, previous_at


if __name__ == "__main__":
    raise SystemExit(main())
