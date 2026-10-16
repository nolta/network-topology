"""SFP identity and diagnostics (DDM: temperature, laser bias, Tx/Rx
optical power) from the Planet web interface.

These switches don't expose SFP diagnostics over SNMP (no ENTITY-SENSOR-MIB
rows, nothing in the vendor MIBs), and their JSON-RPC API only reports SFP
identity and alarm flags. The web UI's "SFP Module Information" page
(sfp_info.htm) gets its numbers from a plain-text endpoint, /stat/sfpinfo,
which accepts the same HTTP Basic login as the rest of the web UI. We only
ever GET it: a POST to the same URL changes the switch's DDM trap settings.

Response format (one line):

    <trapEnabled>/<trapTemp>?<row>|<row>|...

with each row "/"-separated:

    port/type/speed/wavelength_nm/distance_m/temp/voltage/bias/tx/rx

"--" means the module doesn't report that field (or the slot is empty).
Port is the web UI's port number, which matches the bridge port number.
Raw units, per the page's own script: temperature in 1/256 degC (the
page divides by 255, a slip), voltage in 100 uV, bias in 2 uA, Tx/Rx
power already in dBm x 10000. The type/speed fields are decoded from the
module's EEPROM by the switch and are unreliable (an FS 10GBASE-LR module
shows as "100Base-FX"/"10G-Base"), so identity comes from JSON-RPC instead.

Module identity (vendor, part number, revision, serial, type) and the
Loss of Signal / Tx Fault alarms come from the JSON-RPC method
port.status.get at /json_rpc, which accepts the same login. Its rows are
keyed by short interface names like "Gi 1/23" or "2.5G 1/2".
"""
from __future__ import annotations

import base64
import json
import logging
import ssl
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

SFPINFO_PATH = "/stat/sfpinfo"


class SfpError(RuntimeError):
    pass


def _number(field: str, scale: float) -> float | None:
    field = field.strip()
    if not field or field == "--":
        return None
    try:
        return round(int(field) * scale, 4)
    except ValueError:
        return None


def _text(field: str) -> str:
    field = field.strip()
    return "" if field == "--" else field


def parse_sfpinfo(text: str) -> dict[str, dict]:
    """Parse a /stat/sfpinfo response into {port_number: readings}, for
    slots that have a module in them. Readings the module doesn't
    report are None."""
    text = text.strip()
    if "?" not in text:
        raise SfpError(f"unexpected sfpinfo response: {text[:80]!r}")
    _, rows = text.split("?", 1)
    result = {}
    for row in rows.split("|"):
        fields = row.split("/")
        if len(fields) < 10:
            continue
        port = fields[0].strip()
        if all(f.strip() == "--" for f in fields[1:]):
            continue  # empty slot
        result[port] = {
            "type": _text(fields[1]),
            "speed": _text(fields[2]),
            "wavelength_nm": _number(fields[3], 1),
            "distance_m": _number(fields[4], 1),
            "temperature_c": _number(fields[5], 1 / 256),
            "voltage_v": _number(fields[6], 1 / 10000),
            "bias_ma": _number(fields[7], 2 / 1000),
            "tx_dbm": _number(fields[8], 1 / 10000),
            "rx_dbm": _number(fields[9], 1 / 10000),
        }
    return result


# JSON-RPC SFPType enum -> conventional name. Unknown values pass through.
_SFP_TYPE_NAMES = {
    "sfp100FX": "100BASE-FX", "sfp100LX": "100BASE-LX", "sfp100ZX": "100BASE-ZX",
    "sfp100BaseBx10": "100BASE-BX10", "sfp100BaseT": "100BASE-T",
    "sfp1000BaseT": "1000BASE-T", "sfp1000BaseBx10": "1000BASE-BX10",
    "sfp1000BaseCx": "1000BASE-CX", "sfp1000BaseSx": "1000BASE-SX",
    "sfp1000BaseLx": "1000BASE-LX", "sfp1000BaseX": "1000BASE-X",
    "sfp2G5": "2.5G", "sfp5G": "5G", "sfp10G": "10G", "sfp10GDAC": "10G DAC",
    "sfp10GSR": "10GBASE-SR", "sfp10GLR": "10GBASE-LR", "sfp10GLRM": "10GBASE-LRM",
    "sfp10GER": "10GBASE-ER",
}


def _https_request(host, path, username, password, timeout, body: bytes | None = None) -> str:
    """GET (or POST, when body is given) https://<host><path> with HTTP
    Basic auth. The switches use self-signed certificates, so the
    certificate isn't verified."""
    url = f"https://{host}{path}"
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    headers = {"Authorization": f"Basic {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers)
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    logger.debug("%s %s", "POST" if body is not None else "GET", url)
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise SfpError(f"{host}: web login rejected (HTTP 401); check web_username/web_password") from exc
        raise SfpError(f"{host}: {url} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise SfpError(f"{host}: couldn't fetch {url}: {exc}") from exc
    logger.debug("%s OK in %.2fs", url, time.monotonic() - start)
    return text


def fetch_sfpinfo(host: str, username: str, password: str, timeout: float) -> dict[str, dict]:
    """GET /stat/sfpinfo and parse it: {port_number: DDM readings}."""
    body = _https_request(host, SFPINFO_PATH, username, password, timeout)
    if body.lstrip().startswith("<"):
        raise SfpError(f"{host}: {SFPINFO_PATH} returned a web page, not SFP data (login or firmware difference?)")
    return parse_sfpinfo(body)


def parse_port_status(rows: list) -> dict[str, dict]:
    """port.status.get result -> {interface key ("Gi 1/23"): identity and
    alarms}, for ports with an SFP module present."""
    result = {}
    for row in rows:
        key, val = row.get("key"), row.get("val") or {}
        if not key or not val.get("Present"):
            continue
        sfp_type = val.get("SFPType") or ""
        result[key] = {
            "module_type": _SFP_TYPE_NAMES.get(sfp_type, sfp_type),
            "vendor": (val.get("SFPVendorName") or "").strip(),
            "part_number": (val.get("SFPVendorPN") or "").strip(),
            "revision": (val.get("SFPVendorRev") or "").strip(),
            "serial": (val.get("SFPVendorSN") or "").strip(),
            "loss_of_signal": bool(val.get("LossOfSignal")),
            "tx_fault": bool(val.get("TxFault")),
        }
    return result


def fetch_port_status(host: str, username: str, password: str, timeout: float) -> dict[str, dict]:
    """POST the read-only JSON-RPC call port.status.get and parse it."""
    request = json.dumps({"method": "port.status.get", "params": [], "id": 1}).encode()
    body = _https_request(host, "/json_rpc", username, password, timeout, body=request)
    try:
        response = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SfpError(f"{host}: /json_rpc returned something other than JSON") from exc
    if response.get("error"):
        raise SfpError(f"{host}: port.status.get failed: {response['error'].get('message', response['error'])}")
    return parse_port_status(response.get("result") or [])


def match_interface(key: str, ifnames: dict[str, str]) -> str | None:
    """Map a short JSON-RPC interface key ("Gi 1/23", "2.5G 1/2") to the
    ifIndex whose ifName abbreviates to it ("GigabitEthernet 1/23",
    "2.5GigabitEthernet 1/2")."""
    try:
        prefix, number = key.split()
    except ValueError:
        return None
    for ifindex, name in ifnames.items():
        parts = name.split()
        if len(parts) == 2 and parts[1] == number and parts[0].startswith(prefix):
            return ifindex
    return None
