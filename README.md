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

- Requires `snmpbulkwalk`/`snmpget` from net-snmp on your PATH (already
  present on this machine).
- All OIDs used are numeric, so no MIB files need to be installed.
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
