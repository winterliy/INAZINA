"""Download the DB-IP City Lite database used to place admin logins (country · region · city).

    python3 tools/update_geoip.py          # fetch this month's file (or last month's)
    python3 tools/update_geoip.py --check  # print the installed file's date and a sample lookup

DB-IP publishes a free monthly City Lite database under CC BY 4.0; it needs no account.
Attribution ("IP Geolocation by DB-IP") is shown wherever the data is displayed.
The file is replaced only after the download opens and answers a lookup.
"""
import argparse
import datetime
import gzip
import os
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = Path(os.environ.get("INAZINA_GEOIP_PATH", ROOT / "database" / "geoip" / "dbip-city-lite.mmdb"))
URL = "https://download.db-ip.com/free/dbip-city-lite-{month}.mmdb.gz"


def months(today=None):
    """This month, then last month (a new file appears early in the month)."""
    today = today or datetime.date.today()
    first = today.replace(day=1)
    previous = (first - datetime.timedelta(days=1)).replace(day=1)
    return [first.strftime("%Y-%m"), previous.strftime("%Y-%m")]


def verify(path):
    import maxminddb
    with maxminddb.open_database(str(path)) as reader:
        if "city" not in reader.metadata().database_type.lower():
            raise ValueError(f"unexpected database type: {reader.metadata().database_type}")
        if not reader.get("8.8.8.8"):
            raise ValueError("sample lookup returned nothing")
        return datetime.datetime.fromtimestamp(reader.metadata().build_epoch, datetime.timezone.utc)


def download():
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    for month in months():
        url = URL.format(month=month)
        try:
            with tempfile.NamedTemporaryFile(dir=TARGET.parent, suffix=".mmdb", delete=False) as out:
                temp = Path(out.name)
                request = urllib.request.Request(url, headers={"User-Agent": "inazina-geoip-updater/1.0"})
                with urllib.request.urlopen(request, timeout=120) as response, gzip.GzipFile(fileobj=response) as data:
                    shutil.copyfileobj(data, out)
        except OSError as exc:
            print(f"{month}: {exc}", file=sys.stderr)
            temp.unlink(missing_ok=True)
            continue
        try:
            built = verify(temp)
        except Exception:
            temp.unlink(missing_ok=True)
            raise
        os.replace(temp, TARGET)
        print(f"설치 완료: {TARGET} (DB-IP City Lite {month}, 빌드 {built:%Y-%m-%d})")
        return 0
    print("GeoIP 데이터베이스를 받지 못했습니다.", file=sys.stderr)
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="show the installed database instead of downloading")
    if parser.parse_args().check:
        if not TARGET.exists():
            print(f"설치되지 않음: {TARGET}")
            return 1
        built = verify(TARGET)
        sys.path.insert(0, str(ROOT))
        from web_api.admin_logins import location
        print(f"{TARGET} · 빌드 {built:%Y-%m-%d} · 8.8.8.8 → {location('8.8.8.8')}")
        return 0
    return download()


if __name__ == "__main__":
    sys.exit(main())
