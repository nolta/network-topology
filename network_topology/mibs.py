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
DOT1D_TP_FDB_PORT = ".1.3.6.1.2.1.17.4.3.1.2"  # index = 6-byte MAC -> bridge port
DOT1D_TP_FDB_STATUS = ".1.3.6.1.2.1.17.4.3.1.3"  # index = 6-byte MAC -> status (3 = learned)

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
