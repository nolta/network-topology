# network-topology

Maps an Ethernet network by polling a set of SNMP-enabled layer-3 switches
for LLDP neighbor tables (switch-to-switch and switch-to-LLDP-device links)
and bridge forwarding tables + ARP (for dumb end hosts LLDP doesn't cover).

Outputs:
- `output/devices.csv` — every discovered device
- `output/links.csv` — every discovered link with the exact port on each end
- `output/topology.dot` — Graphviz source (render with `dot -Tsvg` if you have
  Graphviz installed, or paste into an online renderer)
- `output/topology.png` — a diagram rendered directly with networkx/matplotlib
  (no Graphviz binary required)
- `output/port_health.csv` — per-port error counters and link state, with
  what changed since the previous run (see [Port health](#port-health))

## Setup

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml
# edit config.yaml: set your real SNMPv2c community string
```

`config.yaml` is gitignored so the community string never gets committed.

## Run

```
source .venv/bin/activate
python -m network_topology --config config.yaml --output output
```

Switches are collected **concurrently** (up to `max_concurrency` at once),
so the total runtime is bound by the slowest switch rather than the sum of
all of them. Over a high-latency path this is a big win. Note that higher
concurrency also multiplies the packet rate through a shared uplink (e.g. a
VPN/subnet router), which can *increase* loss and retries -- if you see
that, lower `max_concurrency`. Set it to `1` for strictly serial collection
(and tidy, non-interleaved `-v` output).

Add `-v`/`--verbose` to see every individual SNMP request as it happens --
target host, table name, parameters, timing, and success/failure for each
one. Useful for pinpointing exactly where and why a switch's collection is
failing (which table, how long it took before timing out, etc.) rather than
just seeing the final summary error. Every log line names its host, so it
stays traceable even when switches are collected concurrently:

```
python -m network_topology --config config.yaml --output output -v
```

### Re-plotting without re-collecting

Each run caches the raw collected data to `<output>/collected.json`. To
regenerate the diagram and CSVs from that cache -- instantly, with no SNMP
collection and no network access -- pass `--replot`:

```
python -m network_topology --output output --replot
```

This is the fast way to iterate on the diagram (e.g. after tweaking colors
or layout) or to re-run the link-inference logic without waiting on the
(slow, high-latency) collection. Because the *raw* per-switch data is
cached -- not the finished diagram -- a re-plot also picks up any code
changes to the topology model or renderer. The log line reports when the
cached data was collected, so you know how stale it is. `--replot` needs no
config file.

## Port health

Every collection also reads each physical port's error counters
(`ifInErrors`/`ifOutErrors`, plus EtherLike FCS, symbol and alignment
errors), its link state and last link change, and its media type (MAU-MIB,
e.g. `1000BASE-T` vs fiber `1000BASE-X`). The counters are cumulative since
the switch booted, so on its own a snapshot only says "N errors, ever". What
matters on a marginal link is whether errors are *still increasing*, so each
run is compared against the previous run's `collected.json`. A port is
flagged when, since that run:

- any error counter increased,
- its link went down, or
- its link went down and came back (link changed).

Flagged ports are printed at the end of the run (with `[fiber]` and the
peer on the other end, when known), and every active port is written to
`port_health.csv` with both totals (`total_*`) and new-since-last-run
(`new_*`) counts. A switch reboot between runs is detected from
`sysUpTime`: counters then restart from zero, so everything counted since is
treated as new, and links coming up right after the boot aren't flagged.
The first run (or the first after upgrading from a cache without port
stats) has nothing to compare against, so it just records a baseline and
mentions links that came up in the last 24 hours. `--replot` doesn't touch
`port_health.csv`.

To use it as a monitor, run the collection periodically (e.g. from cron);
each run reports what changed since the one before.

### SFP diagnostics

Error counters only move once a fiber link is already corrupting frames.
The optical readings warn earlier, but these switches don't expose them
over SNMP (no ENTITY-SENSOR-MIB rows, nothing in the vendor MIBs), and
their JSON-RPC API only reports SFP identity and alarm flags. The web UI's
"SFP Module Information" page does show them, and gets them from a
plain-text endpoint, `https://<switch>/stat/sfpinfo`. If `web_username` and
`web_password` are set in `config.yaml`, each run reads that endpoint (GET
only; a POST to it changes the switch's DDM trap settings) and records each
module's wavelength, rated distance, temperature, supply voltage, laser
bias and Tx/Rx power. Use a read-only web account. The switches'
certificates are self-signed, so they aren't verified.

Each run also calls the read-only JSON-RPC method `port.status.get`
(`https://<switch>/json_rpc`, same login) for every module's vendor, part
number, revision, serial number and type, and its Loss of Signal / Tx Fault
alarms. (The SFP page's own type/speed fields are decoded unreliably by the
switch, e.g. an FS 10GBASE-LR module shows as "100Base-FX", so they aren't
used.)

The readings are printed in a short "SFP modules" summary and added to
`port_health.csv` (`sfp_*` columns). When both ends of a switch-to-switch
link report power, `link_loss_db` is the far end's Tx minus this end's Rx,
i.e. the actual loss in that direction. A port is flagged when its SFP:

- receives less than `sfp_rx_min_dbm` (default -18 dBm; typical 1G
  receivers work down to about -20 dBm),
- receives `sfp_rx_drop_db` (default 2 dB) less than in the previous run, or
- runs at `sfp_temp_max_c` (default 65 C) or hotter; standard SFPs are
  rated 0-70 C, industrial ones (e.g. part numbers ending `-I`) usually
  -40-85 C, so raise this if all your modules are industrial,
- reports Loss of Signal (while the link is up, this means the received
  light is at the module's own threshold), or
- reports a Tx fault.

Note that the Rx threshold assumes 1G optics. A 10G module running at 1G
(as on the `.10`-`.14` link) is only specified down to about -14.4 dBm, so
check the module's datasheet and set `sfp_rx_min_dbm` accordingly.

A failure to read SFP data (wrong login, unreachable web UI) is logged as a
warning and doesn't affect the rest of that switch's collection.

## How link discovery works

1. **LLDP-MIB** (`lldpRemTable`) is walked on every switch first. This is
   authoritative: it gives the exact local and remote port for every link
   between two LLDP-speaking devices (switch-to-switch, and switch to any
   LLDP-capable endpoint like an AP or VoIP phone).
2. For ports LLDP has no data on, the **Q-BRIDGE-MIB forwarding table**
   (`dot1qTpFdbTable`) is used to see which MAC addresses were learned on
   each port, cross-referenced against the **ARP table**
   (`ipNetToMediaTable`) to resolve MACs to IPs where possible. (The tool
   prefers the per-VLAN Q-BRIDGE table over the legacy MAC-only
   `dot1dTpFdbTable` and only falls back to the latter if a switch doesn't
   implement Q-BRIDGE -- see the note below on why.)
   - A port with exactly one learned MAC is a confirmed direct host link.
   - A port with more than one learned MAC is ambiguous on its own (could
     be an unmanaged switch/hub, or a trunk toward another of your known
     switches). The tool votes: of the MACs behind that port, how many
     have a *confirmed* single-MAC home elsewhere in the network? If one
     switch wins clearly (≥50% of the MACs, ≥2 votes, and a solid margin
     over the runner-up), the port is reported as an inferred
     switch-to-switch uplink (drawn as a thick orange edge). Otherwise it
     stays an "unidentified device" node, with the vote breakdown kept in
     its notes as a hint (e.g. "possible uplink toward: 10.10.50.11:
     9/50") for manual tracing. In the diagram this node is drawn as an
     orange pseudo-node with a **single** link to the switch and the
     individual MACs fanned out behind it -- making explicit that those
     devices share one physical uplink (an inferred unmanaged switch/hub,
     or one device presenting several MACs). In `devices.csv` it stays a
     single row (MACs listed in its `notes`).
   - This correlation only works when a switch has *some* confirmed leaf
     hosts to vote with, and breaks down on ports whose traffic passes
     through multiple further switch hops (the vote gets split across
     many candidates). If most of your big ports stay "unidentified",
     enabling LLDP transmit/receive on the switches (usually under an
     L2/Discovery settings page) will resolve them with certainty instead
     of correlation guesswork.

## Notes

- **No external SNMP tools required.** SNMPv2c is spoken directly over UDP
  by a small built-in engine (`network_topology/_snmpv2c.py`, pure Python,
  no dependencies) -- net-snmp does **not** need to be installed. Only
  SNMPv2c is supported (no v1, no v3).
- All OIDs used are numeric, so no MIB files are involved either.
- **Why Q-BRIDGE instead of the classic BRIDGE-MIB FDB:** the legacy
  `dot1dTpFdbTable` is indexed by MAC address only. A VLAN-aware switch
  has to synthesize it in software from its per-VLAN hardware FDB and
  re-sort by MAC for each lexicographic SNMP walk step -- an O(n^2)
  operation on the management CPU. On the 24-port Planet IGS-6325 switches
  in this network with large FDBs, this pegged the CPU to 100% and timed
  the walk out entirely (~16s to failure), which in turn starved the SNMP
  agent and caused *seemingly random* collateral timeouts on whatever
  request happened to run next. The per-VLAN `dot1qTpFdbTable` matches the
  hardware layout and returned the same data in ~1s with no CPU spike.
  Switching to it resolved the intermittent collection failures that
  earlier request-level tuning (timeouts, retries, `max_repetitions`,
  pacing) could not.
- If a switch still doesn't respond within `timeout_seconds` × `retries`,
  its whole collection is retried up to `switch_retries` times before
  being skipped with a warning. Over a high-latency/lossy path (e.g. these
  switches are reached via a ~165ms Tailscale subnet route) this is cheap
  insurance against occasional packet loss.
- `max_repetitions` (rows per GETBULK PDU) and `pace_seconds` (delay
  between each table walk on a switch, default 0) are available for
  gentler treatment of a weak agent, but shouldn't be needed now that the
  FDB walk is cheap.
- `dot1qTpFdbStatus`/`dot1dTpFdbStatus` (used to filter FDB entries to
  "learned" ones) is treated as optional: if it fails, all FDB entries are
  used unfiltered rather than losing the whole switch's data over one
  refinement column.
