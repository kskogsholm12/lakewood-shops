"""Find a Google Street View camera position facing each shop that has no storefront photo yet.

For every shop with a street address but no storefront photo, this asks Google's Street View
*metadata* endpoint (free, no image is downloaded) for the nearest outdoor panorama to the shop's
map point (from data/geocache.csv). It then works out the compass heading from that panorama to the
shop, so the camera looks at the shop's front, and saves one row per shop in data/streetview.csv:

    name, pano_id, heading, pitch, fov, date, status

No image bytes and no URL containing the key are ever stored. The app turns these rows into a
storefront photo (fetched server-side, so the key never reaches the browser), and the Review photos
page lets you turn the camera or reject a view.

The key is read from .streamlit/secrets.toml (GOOGLE_MAPS_API_KEY = "...") and is never printed.

    .venv/bin/python streetview.py          # only shops not yet in data/streetview.csv
    .venv/bin/python streetview.py --all    # redo every shop (views you rejected stay rejected)
"""
import csv
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
BUSINESSES = ROOT / "data" / "businesses.csv"
GEOCACHE = ROOT / "data" / "geocache.csv"
PHOTOS = ROOT / "data" / "photos.csv"
MY_PHOTOS = ROOT / "data" / "photos"
OUT = ROOT / "data" / "streetview.csv"
SECRETS = ROOT / ".streamlit" / "secrets.toml"
FIELDS = ["name", "pano_id", "heading", "pitch", "fov", "date", "status"]
# Overridable only so the script can be tested against a local stand-in server.
METADATA_URL = os.environ.get("LAKEWOOD_SV_METADATA", "https://maps.googleapis.com/maps/api/streetview/metadata")


def api_key():
    if os.environ.get("GOOGLE_MAPS_API_KEY"):
        return os.environ["GOOGLE_MAPS_API_KEY"].strip()
    if SECRETS.exists():
        m = re.search(r'^\s*GOOGLE_MAPS_API_KEY\s*=\s*["\']([^"\']+)["\']', SECRETS.read_text(), re.M)
        if m:
            return m.group(1).strip()
    return None


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def has_storefront(shop, photo_rows):
    if any(r["name"] == shop["name"] and r.get("photo_url") and r.get("type") == "storefront" for r in photo_rows):
        return True
    folder = MY_PHOTOS / slug(shop["name"])
    if folder.is_dir() and any(("storefront" in f.stem.lower() or "front" in f.stem.lower()) for f in folder.iterdir()):
        return True
    return any(u.strip().startswith("storefront: ") for u in (shop.get("photo_urls") or "").split(" ; "))


def bearing(lat1, lon1, lat2, lon2):
    """Compass heading (0 = north, 90 = east) from point 1 to point 2."""
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def metadata(lat, lon, key):
    q = urllib.parse.urlencode({"location": f"{lat},{lon}", "source": "outdoor", "radius": 60, "key": key})
    req = urllib.request.Request(f"{METADATA_URL}?{q}", headers={"User-Agent": "shop-local-lakewood/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:  # message never includes the URL (and so never the key)
        return {"status": f"HTTP_{e.code}"}
    except Exception as e:
        return {"status": "NETWORK_ERROR", "error": type(e).__name__}


def main():
    redo = "--all" in sys.argv
    key = api_key()
    if not key:
        sys.exit("No GOOGLE_MAPS_API_KEY in .streamlit/secrets.toml, so nothing to do.")
    shops = list(csv.DictReader(open(BUSINESSES, newline="")))
    photo_rows = list(csv.DictReader(open(PHOTOS, newline=""))) if PHOTOS.exists() else []
    coords = {r["address"]: r for r in csv.DictReader(open(GEOCACHE, newline=""))} if GEOCACHE.exists() else {}
    old = {r["name"]: r for r in csv.DictReader(open(OUT, newline=""))} if OUT.exists() else {}

    rows, asked, counts = [], 0, {}
    for shop in shops:
        name, prev = shop["name"], old.get(shop["name"])
        if prev and (not redo or prev["status"] == "rejected"):
            rows.append(prev)  # keep, including any turns/rejects made on the Review photos page
            continue
        if not shop.get("address") or has_storefront(shop, photo_rows):
            continue
        g = coords.get(shop["address"])
        if not g or not g.get("lat"):
            row = {"name": name, "status": "no map point"}
        else:
            lat, lon = float(g["lat"]), float(g["lon"])
            m = metadata(lat, lon, key)
            asked += 1
            status = m.get("status", "UNKNOWN")
            if status in ("REQUEST_DENIED", "HTTP_403"):
                sys.exit("Google refused the key (REQUEST_DENIED). Check that the Street View Static API is "
                         "enabled for it and that any key restrictions allow this use. Nothing was saved.")
            if status == "OVER_QUERY_LIMIT":
                sys.exit("Google says the query limit is reached; try again later. Nothing was saved.")
            if status == "OK":
                loc = m.get("location", {})
                head = bearing(loc.get("lat", lat), loc.get("lng", lon), lat, lon)
                row = {"name": name, "pano_id": m.get("pano_id", ""), "heading": f"{head:.0f}", "pitch": "5",
                       "fov": "80", "date": m.get("date", ""), "status": "OK"}
            else:
                row = {"name": name, "status": status}
            time.sleep(0.1)
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        print(f"{name}: {row['status']}" + (f", heading {row['heading']}°, {row.get('date') or 'no date'}"
                                            if row["status"] == "OK" else ""))
        rows.append(row)

    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"\nAsked Google about {asked} shop(s): " + ", ".join(f"{v} {k}" for k, v in counts.items())
          + f". Saved to {OUT}")


if __name__ == "__main__":
    main()
