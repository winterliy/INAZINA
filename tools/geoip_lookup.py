"""Print the location of IP addresses from the local DB-IP City Lite file, as JSON.

    python3 tools/geoip_lookup.py 8.8.8.8 1.1.1.1   ->  {"8.8.8.8": "Mountain View, California, US", ...}

Loopback and private addresses read "로컬"; an address the file does not know, or a missing
file, reads "알 수 없음". Used by the WL viewer's entry log (apps/wl/vite.config.ts).
"""
import ipaddress
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = Path(os.environ.get("INAZINA_GEOIP_PATH", ROOT / "database" / "geoip" / "dbip-city-lite.mmdb"))


def describe(record):
    if not record:
        return "알 수 없음"
    name = lambda key: ((record.get(key) or {}).get("names") or {}).get("en")
    region = (record.get("subdivisions") or [{}])[0].get("names", {}).get("en")
    country = (record.get("country") or {}).get("iso_code")
    return ", ".join(part for part in (name("city"), region, country) if part) or "알 수 없음"


def main(ips):
    out, reader = {}, None
    try:
        import maxminddb
        reader = maxminddb.open_database(str(DB)) if DB.exists() else None
    except ImportError:
        pass
    for ip in ips:
        try:
            address = ipaddress.ip_address(ip.removeprefix("::ffff:"))
        except ValueError:
            out[ip] = "알 수 없음"
            continue
        if address.is_loopback or address.is_private or address.is_link_local:
            out[ip] = "로컬"
        else:
            out[ip] = describe(reader.get(str(address))) if reader else "알 수 없음"
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1:])
