"""Collect photos for each business from its own website.

For every row in data/businesses.csv this looks in two public places:
  * the shop's own website: homepage plus a few linked pages (About, Gallery,
    Visit), preferring photos whose file name or alt text suggests a storefront;
  * the lead photo of any news/magazine article in source_urls that is about
    that one shop (round-up articles are skipped so photos don't get mixed up).
It saves the photo URLs in data/photos.csv, storefront shots first. Images are not downloaded or copied: the app shows
them straight from the shop's site and credits the page they came from.

It respects each site's robots.txt, skips logos/icons/tiny images, and keeps
up to MAX_PER_SHOP photos per shop. Re-run any time:

    .venv/bin/python fetch_photos.py            # only shops not yet in photos.csv
    .venv/bin/python fetch_photos.py --all      # refresh every shop

Your own photos always win over these: see README ("Adding your own photos").
"""
import csv
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).parent
BUSINESSES = ROOT / "data" / "businesses.csv"
OUT = ROOT / "data" / "photos.csv"
UA = "Mozilla/5.0 (compatible; shop-local-lakewood/1.0; personal directory)"
MAX_PER_SHOP = 5
MIN_BYTES = 20_000  # smaller files are almost always logos or icons
SUBPAGE_WORDS = re.compile(r"about|gallery|visit|our-?store|location|shop-?local|story|photos", re.I)
STOREFRONT_WORDS = re.compile(r"store-?front|shop-?front|exterior|outside|facade|building|window|signage|"
                             r"our-?store|our-?shop|the-?shop|the-?store|location|visit|\bfront\b", re.I)
# Article URLs like ".../14-shops-to-visit..." or ".../4-new-businesses..." are round-ups of many shops.
ROUNDUP_URL = re.compile(r"/\d+-[a-z]", re.I)
SKIP_WORDS = re.compile(
    r"logo|icon|favicon|sprite|pixel|badge|payment|visa|mastercard|paypal|placeholder|avatar|"
    r"spinner|loading|arrow|button|banner-ad|emoji|flag|social|facebook|instagram|twitter|tiktok|"
    r"_small\.|_thumb|_\d{2,3}x\d{0,3}\.|\b\d{2}x\d{2}\b", re.I)


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta_images, self.images, self.links, self.logos = [], [], [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and (a.get("property") or a.get("name", "")).lower() in (
                "og:image", "og:image:url", "og:image:secure_url", "twitter:image"):
            if a.get("content"):
                self.meta_images.append(a["content"])
        elif tag == "link" and "apple-touch-icon" in (a.get("rel") or "").lower() and a.get("href"):
            self.logos.append(a["href"])
        elif tag == "img" and re.search(r"logo", " ".join(a.get(k) or "" for k in ("src", "alt", "class", "id")), re.I):
            src = a.get("data-src") or a.get("src")
            if src:
                self.logos.append(src)
        elif tag == "img":
            for w, h in ((a.get("width"), a.get("height")),):
                if (w and w.isdigit() and int(w) < 250) or (h and h.isdigit() and int(h) < 200):
                    return
            src = a.get("data-src") or a.get("src") or ""
            srcset = a.get("data-srcset") or a.get("srcset") or ""
            if srcset:  # take the largest candidate in the srcset
                cands = [c.strip().split(" ") for c in srcset.split(",") if c.strip()]
                def width(c):
                    return int(c[1][:-1]) if len(c) > 1 and c[1][:-1].isdigit() else 0
                best = max(cands, key=width)
                if best and best[0]:
                    src = best[0]
            if src:
                self.images.append((src, a.get("alt", "")))
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])


def get(url, n=600_000):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return r.geturl(), r.headers.get_content_type(), r.read(n)


def image_ok(url):
    """True if the URL serves a real photo (JPEG/PNG/WebP) of a reasonable size."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as r:
            ctype, size = r.headers.get_content_type(), int(r.headers.get("Content-Length") or 0)
        if not size:  # some CDNs don't send a length on HEAD; peek at the body instead
            _, ctype, body = get(url, MIN_BYTES + 1)
            size = len(body)
    except Exception:
        return False
    return ctype in ("image/jpeg", "image/png", "image/webp") and size >= MIN_BYTES


def logo_ok(url):
    """Logos can be small or SVG; just make sure the URL serves an image."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.headers.get_content_type().startswith("image/")
    except Exception:
        return False


def norm(u, base):
    u = urllib.parse.urljoin(base, u.strip())
    if u.startswith("//"):
        u = "https:" + u
    return u.split("#")[0]


def article_photo(url):
    """The lead photo (og:image) of a news/magazine article about this one shop."""
    try:
        final, ctype, body = get(url)
    except Exception:
        return None
    if ctype != "text/html":
        return None
    p = PageParser()
    p.feed(body.decode("utf-8", "ignore"))
    for m in p.meta_images:
        u = norm(m, final)
        if not SKIP_WORDS.search(u) and image_ok(u):
            return (u, final, "article")
    return None


def photos_for(website):
    """Return [(photo_url, page_url, kind)] for one website."""
    try:
        final, ctype, body = get(website)
    except Exception as e:
        print(f"    could not open site: {e}")
        return []
    robots = urllib.robotparser.RobotFileParser(urllib.parse.urljoin(final, "/robots.txt"))
    try:
        robots.read()
    except Exception:
        robots = None
    if robots and not robots.can_fetch(UA, final):
        print("    robots.txt asks bots not to read this site; skipping")
        return []

    host = urllib.parse.urlparse(final).netloc
    pages = [(final, body)]
    p = PageParser()
    p.feed(body.decode("utf-8", "ignore"))
    sub = []
    for href in p.links:
        u = norm(href, final)
        if urllib.parse.urlparse(u).netloc == host and SUBPAGE_WORDS.search(u) and u not in sub and u != final:
            sub.append(u)
    for u in sub[:3]:
        if robots and not robots.can_fetch(UA, u):
            continue
        try:
            _, ct, b = get(u)
            if ct == "text/html":
                pages.append((u, b))
        except Exception:
            pass
        time.sleep(0.3)

    seen, cands = set(), []
    for page_url, b in pages:
        pp = PageParser()
        pp.feed(b.decode("utf-8", "ignore"))
        for src, alt in [(m, "") for m in pp.meta_images] + pp.images:
            u = norm(src, page_url)
            key = re.sub(r"[?&](width|w|h|height|v)=\d+", "", u)
            if (not u.startswith("http") or key in seen or SKIP_WORDS.search(u)
                    or re.search(r"\.(svg|gif|ico)(\?|$)", u, re.I)):
                continue
            seen.add(key)
            cands.append((u, page_url, bool(STOREFRONT_WORDS.search(u + " " + alt))))
    # Storefront-looking photos first, then the site's share image, then the rest (stable order).
    cands.sort(key=lambda c: not c[2])

    found = []
    # The shop's logo: an <img> marked "logo" (src/alt/class/id), else its apple-touch-icon.
    first = PageParser()
    first.feed(body.decode("utf-8", "ignore"))
    logo = None
    for src in first.logos:
        u = norm(src, final)
        if u.startswith("http") and logo_ok(u):
            logo = u
            found.append((u, final, "logo"))
            break
    logo_key = re.sub(r"\?.*$", "", logo or "")
    for u, page_url, is_front in cands[:25]:
        if logo_key and re.sub(r"\?.*$", "", u).split("/")[-1] == logo_key.split("/")[-1]:
            continue  # same file as the logo (often reused as the share image)
        if image_ok(u):
            found.append((u, page_url, "storefront" if is_front else "website"))
            if len(found) >= MAX_PER_SHOP + (1 if logo else 0):
                break
    return found


def main():
    refresh_all = "--all" in sys.argv
    with open(BUSINESSES, newline="") as f:
        shops = list(csv.DictReader(f))
    # An article is "about one shop" if no other shop cites it; only those lead photos are used,
    # so a round-up article's photo of one store never lands on another store's card.
    cites = {}
    for s in shops:
        for u in filter(None, (x.strip() for x in s.get("source_urls", "").split(" ; "))):
            cites.setdefault(u, set()).add(s["name"])
    existing = []
    if OUT.exists() and not refresh_all:
        with open(OUT, newline="") as f:
            existing = list(csv.DictReader(f))
    done = {r["name"] for r in existing}
    rows = existing[:]
    for s in shops:
        if s["name"] in done:
            continue
        print(f"{s['name']}: {s.get('website') or '(no website)'}")
        found = photos_for(s["website"]) if s.get("website") else []
        for u in filter(None, (x.strip() for x in s.get("source_urls", "").split(" ; "))):
            if len(cites.get(u, ())) == 1 and not ROUNDUP_URL.search(u) and urllib.parse.urlparse(u).netloc not in (
                    urllib.parse.urlparse(s.get("website") or "").netloc,):
                a = article_photo(u)
                if a and all(a[0] != f[0] for f in found):
                    found.append(a)
                time.sleep(0.3)
        # Storefront shots first, then article lead photos, then other website photos.
        order = {"storefront": 0, "article": 1, "website": 2, "logo": 3}
        found = sorted(found, key=lambda f: order[f[2]])[:MAX_PER_SHOP + 1]
        print(f"    {len(found)} photo(s): " + ", ".join(f[2] for f in found))
        if not found:  # remember the miss so we don't retry every run
            rows.append({"name": s["name"], "photo_url": "", "source_page": s.get("website", ""), "kind": "", "type": "", "rank": "0"})
        for i, (u, page, kind) in enumerate(found, 1):
            rows.append({"name": s["name"], "photo_url": u, "source_page": page, "kind": kind,
                         "type": "logo" if kind == "logo" else "", "rank": str(i)})
        time.sleep(0.5)
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["name", "photo_url", "source_page", "kind", "type", "rank"],
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    names = {r["name"] for r in rows if r["photo_url"]}
    fronts = {r["name"] for r in rows if r.get("kind") == "storefront"}
    print(f"\n{len(names)} of {len(shops)} businesses have photos "
          f"({len(fronts)} with a likely storefront shot). Saved to {OUT}")
    missing = sorted(s["name"] for s in shops if s["name"] not in names)
    if missing:
        print("No photo yet: " + ", ".join(missing))


if __name__ == "__main__":
    main()
