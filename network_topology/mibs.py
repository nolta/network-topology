"""Numeric OIDs for the standard MIBs we rely on.

All OIDs are numeric (not MIB-name based) so this works regardless of what
MIB files happen to be installed locally -- Planet (and any 802.1AB /
RFC-compliant) switches will answer these without any vendor MIB needed.
"""

# SNMPv2-MIB
SYS_NAME = ".1.3.6.1.2.1.1.5.0"
SYS_DESCR = ".1.3.6.1.2.1.1.1.0"

# IF-MIB
IF_DESCR = ".1.3.6.1.2.1.2.2.1.2"  # ifIndex -> ifDescr
IF_PHYS_ADDRESS = ".1.3.6.1.2.1.2.2.1.6"  # ifIndex -> MAC
IF_NAME = ".1.3.6.1.2.1.31.1.1.1.1"  # ifIndex -> ifName (ifXTable, usually short/human like "Gi0/1")

# BRIDGE-MIB
DOT1D_BASE_PORT_IF_INDEX = ".1.3.6.1.2.1.17.1.4.1.2"  # bridge port -> ifIndex
# Legacy (non-VLAN-aware) forwarding table. Indexed by 6-byte MAC only.
# VLAN-aware switches must synthesize this in software from the per-VLAN
# hardware FDB and sort it by MAC for lexicographic SNMP walks -- an
# O(n^2) software operation that pegs the management CPU on switches with
# large FDBs. Prefer the Q-BRIDGE-MIB table below; keep these only as a
# fallback for switches that don't implement Q-BRIDGE.
DOT1D_TP_FDB_PORT = ".1.3.6.1.2.1.17.4.3.1.2"  # index = 6-byte MAC -> bridge port
DOT1D_TP_FDB_STATUS = ".1.3.6.1.2.1.17.4.3.1.3"  # index = 6-byte MAC -> status (3 = learned)

# Q-BRIDGE-MIB (802.1Q). Per-VLAN forwarding table, matching the switch's
# native hardware layout, so the agent serves it cheaply. Index is
# dot1qFdbId(VLAN).<6-byte MAC>. The port value is still a dot1dBasePort,
# so it maps through DOT1D_BASE_PORT_IF_INDEX the same way.
DOT1Q_TP_FDB_PORT = ".1.3.6.1.2.1.17.7.1.2.2.1.2"  # index = VLAN.6-byte MAC -> bridge port
DOT1Q_TP_FDB_STATUS = ".1.3.6.1.2.1.17.7.1.2.2.1.3"  # index = VLAN.6-byte MAC -> status (3 = learned)

# IP-MIB (ARP table)
IP_NET_TO_MEDIA_PHYS_ADDRESS = ".1.3.6.1.2.1.4.22.1.2"  # index = ifIndex.ipAddress -> MAC

# LLDP-MIB (IEEE 802.1AB), OID root 1.0.8802.1.1.2
LLDP_LOC_CHASSIS_ID_SUBTYPE = ".1.0.8802.1.1.2.1.3.1.0"
LLDP_LOC_CHASSIS_ID = ".1.0.8802.1.1.2.1.3.2.0"
LLDP_LOC_PORT_ID_SUBTYPE = ".1.0.8802.1.1.2.1.3.7.1.2"
LLDP_LOC_PORT_ID = ".1.0.8802.1.1.2.1.3.7.1.3"
LLDP_LOC_PORT_DESC = ".1.0.8802.1.1.2.1.3.7.1.4"

LLDP_REM_CHASSIS_ID_SUBTYPE = ".1.0.8802.1.1.2.1.4.1.1.4"
LLDP_REM_CHASSIS_ID = ".1.0.8802.1.1.2.1.4.1.1.5"
LLDP_REM_PORT_ID_SUBTYPE = ".1.0.8802.1.1.2.1.4.1.1.6"
LLDP_REM_PORT_ID = ".1.0.8802.1.1.2.1.4.1.1.7"
LLDP_REM_PORT_DESC = ".1.0.8802.1.1.2.1.4.1.1.8"
LLDP_REM_SYS_NAME = ".1.0.8802.1.1.2.1.4.1.1.9"
LLDP_REM_SYS_DESC = ".1.0.8802.1.1.2.1.4.1.1.10"

# Port ID / Chassis ID subtypes we care about (IEEE 802.1AB)
PORT_ID_SUBTYPE_INTERFACE_ALIAS = "1"
PORT_ID_SUBTYPE_MAC_ADDRESS = "3"
PORT_ID_SUBTYPE_INTERFACE_NAME = "5"
PORT_ID_SUBTYPE_LOCAL = "7"

CHASSIS_ID_SUBTYPE_MAC_ADDRESS = "4"

# Reverse lookup for logging: OID -> friendly constant name (e.g.
# ".1.3.6.1.2.1.17.4.3.1.3" -> "DOT1D_TP_FDB_STATUS").
_OID_TO_NAME = {
    _value: _name
    for _name, _value in list(globals().items())
    if _name.isupper() and isinstance(_value, str) and _value.startswith(".")
}


def name_for(oid: str) -> str:
    """Human-readable name for a known OID, for logging. Falls back to the
    raw OID if it isn't one of the constants defined in this module."""
    return _OID_TO_NAME.get(oid, oid)
