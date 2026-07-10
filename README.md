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

Add `-v`/`--verbose` to see every individual SNMP request as it happens --
target host, table name, parameters, timing, and success/failure for each
one. Useful for pinpointing exactly where and why a switch's collection is
failing (which table, how long it took before timing out, etc.) rather than
just seeing the final summary error:

```
python -m network_topology --config config.yaml --output output -v
```

## How link discovery works

1. **LLDP-MIB** (`lldpRemTable`) is walked on every switch first. This is
   authoritative: it gives the exact local and remote port for every link
   between two LLDP-speaking devices (switch-to-switch, and switch to any
   LLDP-capable endpoint like an AP or VoIP phone).
2. For ports LLDP has no data on, the **BRIDGE-MIB forwarding table**
   (`dot1dTpFdbTable`) is used to see which MAC addresses were learned on
   each port, cross-referenced against the **ARP table**
   (`ipNetToMediaTable`) to resolve MACs to IPs where possible.
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
     9/50") for manual tracing.
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
- If a switch doesn't respond within `timeout_seconds` × `retries`, its
  whole collection is retried up to `switch_retries` times (default 1,
  i.e. no retry) before being skipped with a warning. Some Planet models
  in this network are flaky under sustained SNMP load -- the failure
  isn't tied to one fixed table, it moves around between different OIDs
  from run to run, so `switch_retries` (retrying the whole switch) has
  proven more effective than tuning any single request's parameters.
- `max_repetitions` (rows per GETBULK PDU, default 10) and `pace_seconds`
  (delay between each table walk on a switch, default 0) are also
  available if you want to trade collection speed for gentler treatment
  of a weak/embedded agent, though in this network they didn't resolve
  the flakiness on their own.
- `dot1dTpFdbStatus` (used to filter FDB entries to "learned" ones) is
  treated as optional: if it fails, all FDB entries are used unfiltered
  rather than losing the whole switch's data over one refinement column.
