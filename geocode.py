"""Turn street addresses into map coordinates, cached in data/geocache.csv.

Uses the free US Census geocoder first and OpenStreetMap (Nominatim) as a
fallback. Each address is looked up once; results are saved so the app
doesn't hit the network again. Run `python geocode.py` to fill the cache by
hand, or let the app do it the first time it opens the Map tab.
"""
import csv
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

CACHE = Path(__file__).parent / "data" / "geocache.csv"
UA = {"User-Agent": "shop-local-lakewood/1.0"}


def _get(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def _census(address: str):
    q = urllib.parse.urlencode({"address": address, "benchmark": "Public_AR_Current", "format": "json"})
    m = _get(f"https://geocoding.geo.census.gov/geocoder/locations/onelineaddress?{q}")["result"]["addressMatches"]
    if m:
        c = m[0]["coordinates"]
        return float(c["y"]), float(c["x"])


def _nominatim(address: str):
    q = urllib.parse.urlencode({"q": address, "format": "json", "limit": 1, "countrycodes": "us"})
    m = _get(f"https://nominatim.openstreetmap.org/search?{q}")
    time.sleep(1)  # Nominatim usage policy: max 1 request/second
    if m:
        return float(m[0]["lat"]), float(m[0]["lon"])


def load_cache() -> dict:
    if not CACHE.exists():
        return {}
    with open(CACHE, newline="") as f:
        return {r["address"]: (r["lat"], r["lon"]) for r in csv.DictReader(f)}


def geocode_all(addresses) -> dict:
    """Return {address: (lat, lon)} for every address, looking up only new ones."""
    cache = load_cache()
    new = [a for a in dict.fromkeys(addresses) if a and a not in cache]
    for addr in new:
        latlon = None
        for fn in (_census, _nominatim):
            try:
                latlon = fn(addr)
            except Exception:
                latlon = None
            if latlon:
                break
        cache[addr] = latlon or ("", "")  # remember misses so we don't retry every load
    if new:
        try:
            with open(CACHE, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["address", "lat", "lon"])
                for a, (lat, lon) in sorted(cache.items()):
                    w.writerow([a, lat, lon])
        except OSError:  # read-only hosting (e.g. Streamlit Community Cloud): keep results in memory only
            pass
    return cache


if __name__ == "__main__":
    with open(Path(__file__).parent / "data" / "businesses.csv", newline="") as f:
        addrs = [r["address"] for r in csv.DictReader(f)]
    result = geocode_all(addrs)
    found = sum(1 for a in addrs if a and result.get(a, ("", ""))[0] != "")
    print(f"{found} of {sum(1 for a in addrs if a)} addresses have coordinates. Saved to {CACHE}")
