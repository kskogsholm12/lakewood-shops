"""Shop Local Lakewood: a searchable catalog of small businesses in Lakewood, Ohio.

All data lives in data/businesses.csv. Add a row to add a business, or add a
column to add a new field: new columns show up in the table and detail cards
automatically.
"""
import base64
import csv
import html
import os
import re
import threading
import urllib.parse
import urllib.request
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote_plus, urlparse
from zoneinfo import ZoneInfo

import pandas as pd
import pydeck as pdk
import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx

from geocode import geocode_all

DATA_FILE = Path(__file__).parent / "data" / "businesses.csv"
# Columns with special handling; any other column is shown as-is.
CORE = ["name", "category", "address", "hours", "gift_ideas", "website", "phone", "instagram"]
HIDDEN_IN_CARD = {"name", "source_urls", "status", "last_checked", "photo_urls"}
PHOTOS_FILE = Path(__file__).parent / "data" / "photos.csv"  # filled by fetch_photos.py
MY_PHOTOS = Path(__file__).parent / "data" / "photos"  # your own: data/photos/<shop-slug>/*.jpg
CATEGORY_EMOJI = {
    "Apparel": "👕", "Art & Crafts": "🎨", "Bakery": "🥐", "Bar & Brewery": "🍺", "Books & Comics": "📚",
    "Coffee & Cafe": "☕", "Experiences & Gift Cards": "🎳", "Florist & Plants": "🌷", "Food & Treats": "🍫",
    "Gifts & Local Pride": "🎁", "Home Decor": "🕯️", "Metaphysical": "🔮", "Oddities & Novelty": "🦴",
    "Outdoor & Sporting": "🚲", "Restaurant": "🍽️", "Salon & Spa": "💆", "Smoke Shop & Decor": "🕯️",
    "Toys & Kids": "🧸", "Vintage": "🪞",
}
IMG_STYLE = "width:100%;height:190px;object-fit:cover;border-radius:8px;display:block"

st.set_page_config(page_title="Shop Local Lakewood", page_icon="🎁", layout="wide")
# House rule: no pop-ups, ever. This hides Streamlit's own dialogs too (like "Page not found" for an
# unknown address, which then just shows the main page). Expand things in place instead.
st.html("<style>.stDialog,[data-testid='stDialog']{display:none!important}"
        "body{overflow:auto!important}</style>")


@st.cache_data
def load(mtime: float) -> pd.DataFrame:
    df = pd.read_csv(DATA_FILE, dtype=str).fillna("")
    for col in CORE:
        if col not in df.columns:
            df[col] = ""
    df["street"] = df["address"].str.extract(r"\d+\s+(.*?\b(?:Ave|Rd|Blvd|St)\b)", expand=False).fillna("")
    return df.sort_values("name", key=lambda s: s.str.lower()).reset_index(drop=True)


ALL = load(DATA_FILE.stat().st_mtime)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


PHOTO_TYPES = ("storefront", "interior", "product", "logo", "other")
# Which photo types may be the big picture at the top of a card, best first. Logos never are.
HERO_ORDER = ("storefront", "interior", "product", "other")


@st.cache_data
def load_photos(mtime: float) -> dict:
    """{shop name: [{url, page, type}, ...]} from data/photos.csv (best first by rank).
    `type` is storefront / interior / product / logo / other. Rows with `type` = reject are hidden."""
    if not PHOTOS_FILE.exists():
        return {}
    p = pd.read_csv(PHOTOS_FILE, dtype=str).fillna("")
    p = p[p["photo_url"] != ""].copy()
    if "type" not in p:
        p["type"] = ""
    p = p[p["type"] != "reject"]
    p["rank"] = pd.to_numeric(p["rank"], errors="coerce").fillna(99)
    p = p.sort_values(["name", "rank"])
    return {n: [{"url": r.photo_url, "page": r.source_page, "type": r.type or "other", "main": r.rank == 1}
                for r in g.itertuples()] for n, g in p.groupby("name")}


AUTO_PHOTOS = load_photos(PHOTOS_FILE.stat().st_mtime if PHOTOS_FILE.exists() else 0)

# Google Street View fallback storefronts (rows from streetview.py). Images are fetched here on the
# server with the key from .streamlit/secrets.toml and handed to the page as data URIs, so the key
# never reaches the browser. No key, no file, or a failed fetch: Street View is simply skipped.
SV_FILE = Path(__file__).parent / "data" / "streetview.csv"
SV_FIELDS = ["name", "pano_id", "heading", "pitch", "fov", "date", "status"]
SV_IMAGE_URL = os.environ.get("LAKEWOOD_SV_IMAGE", "https://maps.googleapis.com/maps/api/streetview")


def _maps_key():
    try:
        return st.secrets.get("GOOGLE_MAPS_API_KEY") or None
    except Exception:  # no secrets.toml at all
        return None


@st.cache_data
def load_streetview(mtime: float) -> dict:
    if not SV_FILE.exists():
        return {}
    with open(SV_FILE, newline="") as f:
        return {r["name"]: r for r in csv.DictReader(f)}


SV = load_streetview(SV_FILE.stat().st_mtime if SV_FILE.exists() else 0)


@st.cache_data(ttl=86400, show_spinner=False)
def streetview_image(pano: str, heading: str, pitch: str, fov: str):
    """640x400 Street View image as a data URI, or None. The key is read inside, never cached or shown."""
    key = _maps_key()
    if not (key and pano):
        return None
    q = urllib.parse.urlencode({"size": "640x400", "pano": pano, "heading": heading, "pitch": pitch or 5,
                                "fov": fov or 80, "return_error_code": "true", "key": key})
    try:
        req = urllib.request.Request(f"{SV_IMAGE_URL}?{q}", headers={"User-Agent": "shop-local-lakewood/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status != 200 or not r.headers.get("Content-Type", "").startswith("image/"):
                return None
            return "data:image/jpeg;base64," + base64.b64encode(r.read()).decode()
    except Exception:  # never log the error text: it could carry the request URL
        return None


def streetview_for(name: str):
    """(data URI, credit) for a shop's Street View storefront, or None."""
    r = SV.get(name)
    if not r or r.get("status") != "OK":
        return None
    src = streetview_image(r["pano_id"], r["heading"], r["pitch"], r["fov"])
    return (src, f"© Google Street View{' (' + r['date'] + ')' if r.get('date') else ''}") if src else None


def prefetch_streetview(names):
    """Fetch the Street View images for a page of cards in parallel (first visit of the day only)."""
    if not _maps_key():
        return
    todo = [SV[n] for n in names if n in SV and SV[n].get("status") == "OK"]
    if not todo:
        return
    ctx = get_script_run_ctx()

    def one(r):
        add_script_run_ctx(threading.current_thread(), ctx)
        streetview_image(r["pano_id"], r["heading"], r["pitch"], r["fov"])

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(one, todo))


def _type_from_filename(name: str) -> str:
    low = name.lower()
    for t in PHOTO_TYPES:
        if low.startswith(t) or f"-{t}" in low or f"_{t}" in low:
            return t
    return "storefront" if "front" in low else "interior"


def photos_for(row, streetview=True) -> list:
    """All photos for one shop as [{src, credit, type}], your own first, duplicates removed.
    Your own files: data/photos/<shop-slug>/, typed by file name (storefront-1.jpg, interior-2.jpg,
    logo.png ...). URLs you add in the photo_urls column can be typed as "interior: https://...".
    Then photos found online (data/photos.csv)."""
    out, seen = [], set()

    def add(src, credit, typ, main=False):
        if src and src not in seen:
            seen.add(src)
            out.append({"src": src, "credit": credit, "type": typ if typ in PHOTO_TYPES else "other", "main": main})

    folder = MY_PHOTOS / slug(row["name"])
    if folder.is_dir():
        for f in sorted(folder.iterdir()):
            mime = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".webp": "webp"}.get(f.suffix.lower())
            if mime:
                add(f"data:image/{mime};base64," + base64.b64encode(f.read_bytes()).decode(),
                    "Your photo", _type_from_filename(f.stem))
    for item in (row.get("photo_urls") or "").split(" ; "):
        item = item.strip()
        typ, _, url = item.partition(": ") if re.match(r"^[a-z]+: ", item) else ("other", "", item)
        add(url, f"[Photo source]({url})", typ)
    for ph in AUTO_PHOTOS.get(row["name"], []):
        add(ph["url"], f"[{urlparse(ph['page']).netloc.replace('www.', '')}]({ph['page']})", ph["type"], ph["main"])
    sv = streetview_for(row["name"]) if streetview else None
    if sv:
        add(sv[0], sv[1], "storefront")
        out[-1]["streetview"] = True
    return out


def _credit_html(credit: str) -> str:
    """Turn a markdown credit like [site](url) into a small HTML link."""
    m = re.match(r"^\[(.*?)\]\((.*?)\)$", credit)
    return f"<a href='{html.escape(m[2], quote=True)}' target='_blank' rel='noopener'>{html.escape(m[1])}</a>" if m \
        else html.escape(credit)


SLIDESHOW_CSS = """<style>
.ss{position:relative;border-radius:10px;overflow:hidden;background:#f1ede6}
.ss-track{display:flex;overflow-x:auto;scroll-snap-type:x mandatory;scroll-behavior:smooth;scrollbar-width:none;
  -webkit-overflow-scrolling:touch;height:100%}
.ss-track::-webkit-scrollbar{display:none}
.ss-slide{flex:0 0 100%;scroll-snap-align:start;position:relative;height:100%}
.ss-slide img{width:100%;height:100%;object-fit:cover;display:block}
.ss-cap{position:absolute;right:6px;top:6px;font-size:11px;line-height:1.3;color:#fff;background:rgba(0,0,0,.55);
  padding:2px 7px;border-radius:10px;max-width:70%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ss-cap a{color:#fff;text-decoration:underline}
.ss-btn{position:absolute;top:50%;transform:translateY(-50%);width:32px;height:32px;border-radius:50%;border:0;
  background:rgba(255,255,255,.88);color:#222;font-size:20px;line-height:30px;padding:0;cursor:pointer;
  box-shadow:0 1px 4px rgba(0,0,0,.3);z-index:2}
.ss-btn:hover{background:#fff}
.ss-prev{left:8px}.ss-next{right:8px}
.ss-dots{position:absolute;left:0;right:0;bottom:8px;display:flex;justify-content:center;gap:6px;z-index:2;
  pointer-events:none}
.ss-dot{width:8px;height:8px;border-radius:50%;border:0;padding:0;background:rgba(255,255,255,.55);
  box-shadow:0 0 2px rgba(0,0,0,.6);cursor:pointer;pointer-events:auto}
.ss-dot.on{background:#fff;width:10px;height:10px;margin-top:-1px}
.ss-badge{position:absolute;left:10px;bottom:10px;width:60px;height:60px;object-fit:contain;background:#fff;
  border-radius:50%;padding:6px;box-shadow:0 1px 4px rgba(0,0,0,.35);z-index:3}
.ss-empty{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;color:#6b6257;
  font-size:13px;gap:6px}
.ss-big .ss-btn{width:44px;height:44px;font-size:26px;line-height:42px}
.ss-big .ss-badge{width:84px;height:84px}
.ss-thumbs{display:flex;gap:6px;overflow-x:auto;margin-top:8px;scrollbar-width:thin}
.ss-thumbs img{width:72px;height:54px;object-fit:cover;border-radius:6px;cursor:pointer;opacity:.6;flex:0 0 auto;
  border:2px solid transparent}
.ss-thumbs img.on{opacity:1;border-color:#c0392b}
@media (max-width:640px){.ss-big .ss-badge{width:60px;height:60px}.ss-big .ss-btn{width:36px;height:36px;font-size:22px;line-height:34px}}
[class*="st-key-card-"],[class*="st-key-full-card"]{scroll-margin-top:72px}
.st-key-full-card{position:relative}
.st-key-close-x{position:absolute!important;top:10px;right:10px;z-index:10;width:auto!important}
.st-key-close-x button{width:40px;height:40px;min-height:40px;padding:0;border-radius:50%;background:#fff;
  border:1px solid rgba(0,0,0,.12);box-shadow:0 1px 5px rgba(0,0,0,.25)}
.st-key-close-x button:hover{border-color:#c0392b;color:#c0392b}
.st-key-full-card h1{padding-right:48px}
@media (max-width:640px){.ss-big .ss-cap{top:auto;bottom:26px;max-width:55%}}
</style>"""

# Arrows, dots and thumbnails. Swiping works without this (it's plain CSS scroll-snap).
SLIDESHOW_JS = """<script>
(function(){
  const root = document.getElementById("%s");
  if (!root || root.dataset.ready) return;
  root.dataset.ready = 1;
  const track = root.querySelector(".ss-track");
  const n = track.children.length;
  const marks = [...root.querySelectorAll(".ss-dot"), ...(root.parentElement.querySelectorAll(".ss-thumbs img"))];
  const idx = () => Math.round(track.scrollLeft / track.clientWidth);
  const go = i => track.scrollTo({left: ((i + n) %% n) * track.clientWidth});
  const paint = () => { const i = idx(); marks.forEach(m => m.classList.toggle("on", +m.dataset.i === i)); };
  root.querySelector(".ss-prev")?.addEventListener("click", e => { e.stopPropagation(); go(idx() - 1); });
  root.querySelector(".ss-next")?.addEventListener("click", e => { e.stopPropagation(); go(idx() + 1); });
  marks.forEach(m => m.addEventListener("click", e => { e.stopPropagation(); go(+m.dataset.i); }));
  track.addEventListener("scroll", () => requestAnimationFrame(paint), {passive: true});
  paint();
  %s
})();
</script>"""

# On a small card, a click anywhere that isn't a link, arrow or dot opens the full card.
CARD_CLICK_JS = """const card = root.closest('[class*="st-key-card-"]');
  if (card && !card.dataset.ready) {
    card.dataset.ready = 1;
    card.style.cursor = "pointer";
    card.addEventListener("click", e => {
      if (e.target.closest("a, button, input, summary, details")) return;
      card.querySelector('[class*="st-key-open-"] button')?.click();
    });
  }"""


_SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.S)
_PENDING_JS: list = []  # scripts waiting to run on older Streamlit (see flush_js)


def _html(body: str):
    try:
        st.html(body, unsafe_allow_javascript=True)
    except TypeError:  # Streamlit < 1.52 (e.g. on Python 3.9): show the HTML, run its scripts via flush_js
        rest = _SCRIPT_RE.sub("", body)
        if rest.strip():
            st.html(rest)
        _PENDING_JS.extend(_SCRIPT_RE.findall(body))


def flush_js():
    """Older Streamlit can't run scripts inside st.html. Run the collected ones in the page itself,
    from one hidden component at the very bottom. They are safe to run twice (they mark what they wired)."""
    if not _PENDING_JS:
        return
    import json
    import streamlit.components.v1 as components
    code = json.dumps("\n".join(f"try{{{c}}}catch(e){{console.warn(e)}}" for c in _PENDING_JS)).replace("</", "<\\/")
    _PENDING_JS.clear()
    components.html(f"""<script>
const d = window.parent.document, run = () => {{ const s = d.createElement("script"); s.textContent = {code};
  d.body.appendChild(s); s.remove(); }};
setTimeout(run, 200); setTimeout(run, 1200);
</script>""", height=0)


def show_photos(row, *, big=False, uid=""):
    """Slideshow of a shop's photos (storefront first) with its logo as a round badge.
    Arrows and dots on every screen, swipe on phones. big=True is the full-screen card version."""
    pics = photos_for(row)
    logo = next((p for p in pics if p["type"] == "logo"), None)
    # Real storefront photos first (the "main" one leading), then the Street View storefront,
    # then the rest with the main photo (rank 1 on the Review photos page) leading, then interior, product ...
    def order(p):
        group = 0 if p["type"] == "storefront" and not p.get("streetview") else 1 if p.get("streetview") else 2
        return group, not p["main"], HERO_ORDER.index(p["type"])
    slides = sorted((p for p in pics if p["type"] in HERO_ORDER), key=order)
    uid = uid or "ss-" + slug(row["name"]) + ("-big" if big else "")
    height = "min(460px, 66vw)" if big else "200px"
    badge = f"<img class='ss-badge' src='{logo['src']}' alt='logo'>" if logo else ""
    if slides:
        inner = "".join(
            f"<div class='ss-slide'><img src='{p['src']}' loading='lazy' alt=''>"
            f"<span class='ss-cap'>{'Photo' if p['type'] == 'other' else p['type'].title()} · "
            f"{_credit_html(p['credit'])}</span></div>" for p in slides)
        track = f"<div class='ss-track'>{inner}</div>"
    elif logo:  # no real photo yet: show the logo nicely instead of an empty box
        track = (f"<div class='ss-empty'><img src='{logo['src']}' style='max-width:60%;max-height:60%;"
                 f"object-fit:contain'>Storefront photo coming soon</div>")
        badge = ""
    else:
        track = (f"<div class='ss-empty'><span style='font-size:{90 if big else 60}px'>"
                 f"{CATEGORY_EMOJI.get(row['category'], '🏬')}</span>No photo yet</div>")
    ctrls = ""
    if len(slides) > 1:
        ctrls = ("<button class='ss-btn ss-prev' aria-label='Previous photo'>‹</button>"
                 "<button class='ss-btn ss-next' aria-label='Next photo'>›</button><div class='ss-dots'>"
                 + "".join(f"<button class='ss-dot' data-i='{i}' aria-label='Photo {i + 1}'></button>"
                           for i in range(len(slides))) + "</div>")
    thumbs = ""
    if big and len(slides) > 1:
        thumbs = "<div class='ss-thumbs'>" + "".join(
            f"<img src='{p['src']}' data-i='{i}' loading='lazy' alt=''>" for i, p in enumerate(slides)) + "</div>"
    body = (SLIDESHOW_CSS + f"<div><div id='{uid}' class='ss{' ss-big' if big else ''}' style='height:{height}'>"
            f"{track}{badge}{ctrls}</div>{thumbs}</div>")
    _html(body + SLIDESHOW_JS % (uid, "" if big else CARD_CLICK_JS))


# Categories that go on the "Eat & Drink" page instead of the shopping page.
# Bakeries and food/treat shops stay with the shops, since they sell giftable goods.
DINING = {"Restaurant", "Bar & Brewery", "Coffee & Cafe"}


def open_shop(sl):
    st.query_params["shop"] = sl  # in the address bar, so the browser's Back button closes it too


def close_shop():
    st.session_state["return_to"] = st.query_params.get("shop", "")
    st.query_params.pop("shop", None)


SCROLL_JS = """<script>
setTimeout(() => document.querySelector('%s')?.scrollIntoView({block: "start"}), 150);
</script>"""


# ---- Holiday events and deals (data/holiday.csv). Rows hide themselves once their end date has passed.
HOLIDAY_FILE = Path(__file__).parent / "data" / "holiday.csv"
LOCAL_TZ = ZoneInfo("America/New_York")


@st.cache_data
def load_holiday(mtime: float) -> pd.DataFrame:
    if not HOLIDAY_FILE.exists():
        return pd.DataFrame(columns=["kind", "title", "business", "start", "end", "time", "place", "address",
                                     "details", "source"])
    h = pd.read_csv(HOLIDAY_FILE, dtype=str).fillna("")
    h["start_d"] = pd.to_datetime(h["start"], errors="coerce").dt.date
    h["end_d"] = pd.to_datetime(h["end"].where(h["end"] != "", h["start"]), errors="coerce").dt.date
    return h.dropna(subset=["start_d", "end_d"]).sort_values(["start_d", "end_d", "title"]).reset_index(drop=True)


def today_local() -> dt.date:
    return dt.datetime.now(LOCAL_TZ).date()


def holiday_now() -> pd.DataFrame:
    """Rows that haven't ended yet (Lakewood time)."""
    h = load_holiday(HOLIDAY_FILE.stat().st_mtime if HOLIDAY_FILE.exists() else 0)
    return h[h["end_d"] >= today_local()]


def _md(text: str) -> str:
    """Plain text for st.markdown: keep "$10" from turning into math."""
    return text.replace("$", "\\$")


def _day(d: dt.date) -> str:
    return f"{d:%b} {d.day}"


def date_chip(start: dt.date, end: dt.date, today: dt.date) -> str:
    if start < today:
        return f"Now through {_day(end)}" if end > today else "Today"
    if start == end:
        return "Today" if start == today else f"{start:%a} {_day(start)}"
    if start.month == end.month and start.year == end.year:
        return f"{_day(start)}-{end.day}"
    return f"{_day(start)} - {_day(end)}" + (f", {end.year}" if end.year != start.year else "")


def is_soon(start: dt.date, end: dt.date, today: dt.date) -> bool:
    """Happening today or this weekend (the coming Sat-Sun, or the current one if it's the weekend)."""
    sat = today + dt.timedelta(days=(5 - today.weekday()) % 7) if today.weekday() != 6 else today - dt.timedelta(1)
    return (start <= today <= end) or (start <= sat + dt.timedelta(1) and end >= sat)


def _short(text: str, n: int = 90) -> str:
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def holiday_lines(name: str, full: bool):
    """🎄 lines for one business on its card: the soonest one on a small card, all of them on the full card."""
    h = holiday_now()
    mine = h[h["business"] == name]
    today = today_local()
    for _, r in (mine if full else mine.head(1)).iterrows():
        details = r["details"] if full else _short(r["details"])
        st.markdown(f"🎄 **{_md(r['title'])}**" + (f": {_md(details)}" if details else "")
                    + f" · _{date_chip(r['start_d'], r['end_d'], today)}_")


def shop_info(row, extra_cols, *, full=False):
    """The text part of a card. full=True adds everything (sources, notes, last checked)."""
    if full:
        st.title(row["name"])
    else:
        st.markdown(f"### {row['name']}")
    badge = f"**{row['category']}**" if row["category"] else ""
    if row.get("status") == "needs check":
        badge += "  ⚠️ _needs checking_"
    elif row.get("status") == "online only":
        badge += "  🛒 _online only_"
    st.markdown(badge)
    holiday_lines(row["name"], full)
    if row["gift_ideas"]:
        st.markdown(f"🎁 {row['gift_ideas']}")
    if row["address"]:
        maps = "https://www.google.com/maps/search/?api=1&query=" + quote_plus(f"{row['name']} {row['address']}")
        st.markdown(f"📍 [{row['address']}]({maps})")
    st.markdown(f"🕒 {row['hours'] or '_Hours not listed yet, call or check online_'}")
    if row["phone"]:
        tel = re.sub(r"[^0-9+]", "", row["phone"])
        st.markdown(f"📞 [{row['phone']}](tel:{tel})" if full else f"📞 {row['phone']}")
    if row["website"]:
        st.markdown(f"🌐 [{row['website'].replace('https://', '').rstrip('/')}]({row['website']})")
    if row["instagram"]:
        handle = "@" + row["instagram"].rstrip("/").split("/")[-1].split("?")[0]
        st.markdown(f"📸 [{handle}]({row['instagram']})")
    for c in extra_cols:
        if row[c]:
            st.markdown(f"**{c.replace('_', ' ').title()}:** {row[c]}")
    if full and row.get("last_checked"):
        st.caption(f"Last checked {row['last_checked']}")


def full_card(row, extra_cols, noun):
    """One shop expanded to fill the page: big slideshow, every detail, a small map, and a way back."""
    with st.container(border=True, key="full-card"):
        # Close X, pinned to the card's top-right corner by CSS (no row of its own).
        st.button("", icon=":material/close:", key="close-x", on_click=close_shop, help="Close")
        _html(SCROLL_JS % '[class*="st-key-full-card"]')
        left, right = st.columns([3, 2], gap="medium")
        with left:
            show_photos(row, big=True)
        with right:
            shop_info(row, extra_cols, full=True)
            if row["address"]:
                lat, lon = geocode_all([row["address"]]).get(row["address"], ("", ""))
                if lat != "":
                    st.pydeck_chart(pdk.Deck(
                        map_style="light",
                        initial_view_state=pdk.ViewState(latitude=float(lat), longitude=float(lon), zoom=15),
                        layers=[pdk.Layer("ScatterplotLayer", [{"lat": float(lat), "lon": float(lon)}],
                                          get_position="[lon, lat]", get_fill_color=[192, 57, 43, 230],
                                          get_radius=25, radius_min_pixels=8, stroked=True,
                                          get_line_color=[255, 255, 255], line_width_min_pixels=2)],
                    ), height=220)
            if row.get("source_urls"):
                with st.expander("Where this info comes from"):
                    for u in row["source_urls"].split(" ; "):
                        st.markdown(f"- {u}")


def directory(df, *, title, caption, header, placeholder, noun, key):
    st.title(title)
    st.caption(caption)

    with st.sidebar:
        st.header(header)
        query = st.text_input("Search", placeholder=placeholder, key=f"{key}_q")
        cats = st.multiselect("Category", sorted(c for c in df["category"].unique() if c), key=f"{key}_cat")
        streets = st.multiselect("Street", sorted(s for s in df["street"].unique() if s), key=f"{key}_street")
        show_unverified = st.checkbox("Include listings that need checking", value=True, key=f"{key}_unv")
        show_online = True
        if (df["status"] == "online only").any():
            show_online = st.checkbox("Include online-only shops", value=True, key=f"{key}_online")
        st.divider()
        st.caption(f"{len(df)} {noun}s on this page, {len(ALL)} in the whole catalog. Edit `data/businesses.csv` to add or fix one.")

    view = df
    if query:
        q = query.lower()
        searchable = view.drop(columns=["source_urls"], errors="ignore").agg(" ".join, axis=1).str.lower()
        view = view[searchable.str.contains(q, regex=False)]
    if cats:
        view = view[view["category"].isin(cats)]
    if streets:
        view = view[view["street"].isin(streets)]
    if not show_unverified and "status" in view:
        view = view[view["status"] != "needs check"]
    if not show_online and "status" in view:
        view = view[view["status"] != "online only"]

    extra_cols = [c for c in df.columns if c not in CORE and c not in HIDDEN_IN_CARD and c != "street"]
    jump = st.session_state.pop(f"goto_{key}", None)  # "open this shop" from the Holiday page
    if jump:
        st.query_params["shop"] = jump
    opened = df[df["name"].map(slug) == st.query_params.get("shop", "")]
    if not opened.empty:  # one card expanded to fill the page, instead of the list
        full_card(opened.iloc[0], extra_cols, noun)
        return

    if st.session_state.get("return_to"):  # just closed a card: scroll back to it
        _html(SCROLL_JS % f'[class*="st-key-card-{key}-{st.session_state.pop("return_to")}"]')
    st.subheader(f"{len(view)} {noun}{'s' if len(view) != 1 else ''}")
    tab_cards, tab_map, tab_table = st.tabs(["Cards", "Map", "Table"])

    with tab_cards:
        prefetch_streetview(view["name"].tolist())
        if view.empty:
            st.info(f"No {noun}s match. Try a broader search.")
        for i in range(0, len(view), 3):
            for col, (_, row) in zip(st.columns(3), view.iloc[i : i + 3].iterrows()):
                sl = slug(row["name"])
                with col, st.container(border=True, key=f"card-{key}-{sl}"):
                    show_photos(row)
                    shop_info(row, extra_cols)
                    st.button("See more", key=f"open-{key}-{sl}", icon=":material/open_in_full:", on_click=open_shop, args=(sl,),
                              width="stretch")

    with tab_map:
        with st.spinner("Finding shops on the map (first time only)..."):
            coords = geocode_all(view["address"].tolist())
        pts = view.assign(
            lat=view["address"].map(lambda a: coords.get(a, ("", ""))[0]),
            lon=view["address"].map(lambda a: coords.get(a, ("", ""))[1]),
        )
        pts = pts[pts["lat"].astype(str) != ""].copy()
        missing = len(view) - len(pts)
        if pts.empty:
            st.info("None of these shops could be placed on the map yet.")
        else:
            pts["lat"] = pts["lat"].astype(float)
            pts["lon"] = pts["lon"].astype(float)
            palette = [(230, 25, 75), (60, 180, 75), (0, 130, 200), (245, 130, 48), (145, 30, 180),
                       (70, 200, 200), (240, 50, 230), (150, 170, 20), (200, 120, 120), (0, 128, 128),
                       (160, 110, 230), (154, 99, 36), (128, 0, 0), (90, 160, 110), (128, 128, 0),
                       (230, 170, 90), (0, 0, 117), (128, 128, 128), (220, 190, 40), (40, 40, 40)]
            cat_color = {c: palette[i % len(palette)] for i, c in enumerate(sorted(ALL["category"].unique()))}
            pts["color"] = pts["category"].map(lambda c: list(cat_color[c]) + [220])
            pts["hours_txt"] = pts["hours"].replace("", "Hours not listed")
            st.pydeck_chart(pdk.Deck(
                map_style="light",
                initial_view_state=pdk.ViewState(latitude=41.478, longitude=-81.80, zoom=13.2),
                layers=[pdk.Layer("ScatterplotLayer", pts, get_position="[lon, lat]", get_fill_color="color",
                                  get_radius=45, radius_min_pixels=6, pickable=True,
                                  stroked=True, get_line_color=[255, 255, 255], line_width_min_pixels=1)],
                tooltip={"html": "<b>{name}</b><br/>{category}<br/>{address}<br/>{hours_txt}"},
            ), height=560)
            legend = " · ".join(f"<span style='color:rgb{cat_color[c]}'>●</span> {c}" for c in sorted(pts["category"].unique()))
            st.markdown(legend, unsafe_allow_html=True)
        if missing:
            st.caption(f"{missing} shop(s) aren't on the map because they have no street address or it couldn't be found.")

    with tab_table:
        st.dataframe(
            view.drop(columns=["street"]),
            hide_index=True,
            width="stretch",
            column_config={"website": st.column_config.LinkColumn("website")},
        )
        st.download_button("Download as CSV", view.drop(columns=["street"]).to_csv(index=False), f"lakewood_{key}.csv")


def shops_page():
    directory(ALL[~ALL["category"].isin(DINING)],
              title="🎁 Shop Local Lakewood",
              caption="Small, locally owned shops in Lakewood, Ohio. Keep your holiday money close to home.",
              header="Find a shop", placeholder="e.g. candles, toys, vintage", noun="shop", key="shops")


def dining_page():
    directory(ALL[ALL["category"].isin(DINING)],
              title="🍽️ Eat & Drink Local",
              caption="Locally owned restaurants, bars and cafes in Lakewood. Many sell gift cards, which make easy presents.",
              header="Find a place", placeholder="e.g. pizza, sushi, brunch", noun="place", key="dining")


HOLIDAY_CSS = """<style>
[class*="st-key-hol-soon-"]{background:rgba(192,57,43,.07);border-radius:.5rem}
.hol-chip{display:inline-block;background:#c0392b;color:#fff;border-radius:999px;padding:1px 10px;
  font-size:.82rem;font-weight:600;margin-right:6px;white-space:nowrap}
</style>"""


def _maps_link(place: str, address: str) -> str:
    if not address:
        return _md(place)
    where = address if "Cleveland" in address else f"{address}, Lakewood, OH"
    url = "https://www.google.com/maps/search/?api=1&query=" + quote_plus(where)
    label = address if place in ("", address) else f"{place}, {address}"
    return f"[{_md(label)}]({url})"


def _open_shop_from(page_key: str, sl: str):
    st.session_state[f"goto_{page_key}"] = sl
    st.switch_page(PAGES[page_key])


def holiday_page():
    st.title("🎄 Holidays in Lakewood")
    st.caption("Events and Small Business Weekend deals, from the linked sources. Details can change, "
               "so check with each shop before you go.")
    st.html(HOLIDAY_CSS)
    h, today = holiday_now(), today_local()

    st.subheader("Coming up")
    events = h[h["kind"] == "event"]
    if events.empty:
        st.info("No upcoming holiday events in the list right now.")
    for i, r in events.iterrows():
        soon = is_soon(r["start_d"], r["end_d"], today)
        with st.container(border=True, key=f"hol-{'soon' if soon else 'ev'}-{i}"):
            st.markdown(f"<span class='hol-chip'>{html.escape(date_chip(r['start_d'], r['end_d'], today))}</span> "
                        f"**{html.escape(r['title']).replace('$', '&#36;')}**", unsafe_allow_html=True)
            where = " · ".join(x for x in (f"🕒 {_md(r['time'])}" if r["time"] else "",
                                           f"📍 {_maps_link(r['place'], r['address'])}" if r["place"] or r["address"] else "") if x)
            if where:
                st.markdown(where)
            if r["details"]:
                st.markdown(_md(r["details"]))
            st.caption(f"[Source]({r['source']})")

    st.subheader("Small Business Weekend deals")
    st.caption("Some of these shops aren't in the catalog yet.")
    deals = h[h["kind"] == "deal"]
    if deals.empty:
        st.info("No current deals in the list.")
    known = dict(zip(ALL["name"], ALL["category"]))
    for i, r in deals.iterrows():
        soon = is_soon(r["start_d"], r["end_d"], today)
        with st.container(border=True, key=f"hol-{'soon' if soon else 'deal'}-{i}"):
            st.markdown(f"<span class='hol-chip'>{html.escape(date_chip(r['start_d'], r['end_d'], today))}</span> "
                        f"<b>{html.escape(r['business'] or r['place'])}</b>", unsafe_allow_html=True)
            if r["business"] in known:
                page_key = "dining" if known[r["business"]] in DINING else "shops"
                if st.button(f"See {r['business']}'s card", key=f"hol-open-{i}", type="tertiary",
                             icon=":material/storefront:"):
                    _open_shop_from(page_key, slug(r["business"]))
            st.markdown(f"**{_md(r['title'])}:** {_md(r['details'])}")
            extra = " · ".join(x for x in (f"🕒 {_md(r['time'])}" if r["time"] else "",
                                           f"📍 {_maps_link('', r['address'])}" if r["address"] else "") if x)
            if extra:
                st.markdown(extra)
            st.caption(f"[Source]({r['source']})")


def _rank(r) -> int:
    try:
        return int(float(r["rank"]))
    except (TypeError, ValueError):
        return 99


def _photo_rows() -> list:
    with open(PHOTOS_FILE, newline="") as f:
        return list(csv.DictReader(f))


def _save_photo_rows(rows: list):
    fields = list(rows[0].keys()) if rows else ["name", "photo_url", "source_page", "kind", "type", "rank"]
    with open(PHOTOS_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    load_photos.clear()  # cards pick up the change on their next run


def _set_type(name, url, key):
    new = st.session_state.get(key)
    if not new:  # clicking the selected option again un-selects it; keep the saved type
        return
    rows = _photo_rows()
    for r in rows:
        if r["name"] == name and r["photo_url"] == url:
            r["type"] = new.lower()
    _save_photo_rows(rows)


def _make_main(name, url):
    """Rank 1 for this photo, the shop's other photos keep their order after it."""
    rows = _photo_rows()
    mine = sorted((r for r in rows if r["name"] == name and r["photo_url"]),
                  key=lambda r: (r["photo_url"] != url, _rank(r)))
    for n, r in enumerate(mine, 1):
        r["rank"] = str(n)
    _save_photo_rows(rows)


def _photo_status(names, rows) -> dict:
    """Best kind of photo each shop has: storefront / interior / photos (product, other) / logo / nothing."""
    best = {}
    by_shop = {}
    for r in rows:
        if r["photo_url"] and r["type"] != "reject":
            by_shop.setdefault(r["name"], set()).add(r["type"] or "unreviewed")
    for _, row in ALL[ALL["name"].isin(names)].iterrows():
        kinds = by_shop.get(row["name"], set()) | {p["type"] for p in photos_for(row, False) if p["credit"] == "Your photo"
                                                    or p["credit"].startswith("[Photo source]")}
        best[row["name"]] = next((k for k in ("storefront", "interior") if k in kinds), None) or (
            "photos" if kinds & {"product", "other", "unreviewed"} else "logo" if "logo" in kinds else "nothing")
    return best


def _sv_update(name, *, turn=0, status=None):
    """Turn the Street View camera (degrees) or set its status, saved to data/streetview.csv."""
    with open(SV_FILE, newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        if r["name"] == name:
            if turn:
                r["heading"] = str(round(float(r["heading"] or 0) + turn) % 360)
            if status:
                r["status"] = status
    with open(SV_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SV_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    load_streetview.clear()


def streetview_review(shop, has_storefront):
    r = SV.get(shop)
    if not r and has_storefront:
        return
    with st.container(border=True):
        st.markdown("**Google Street View** (used as the storefront photo when there isn't a real one)")
        if not r:
            st.caption("Not looked up yet. Run `streetview.py` once a Google Maps key is in place.")
            return
        if r["status"] not in ("OK", "rejected"):
            st.caption(f"No outdoor Street View near this address (Google said {r['status']}).")
            return
        rejected = r["status"] == "rejected"
        src = streetview_image(r["pano_id"], r["heading"], r["pitch"], r["fov"]) if _maps_key() else None
        if src:
            st.html(f"<img src='{src}' alt='Street View' style='width:100%;max-width:640px;aspect-ratio:16/10;"
                    f"object-fit:cover;border-radius:8px;display:block{';opacity:.45' if rejected else ''}'>")
        elif not _maps_key():
            st.caption("Street View is off: add GOOGLE_MAPS_API_KEY to .streamlit/secrets.toml to see it.")
        else:
            st.caption("Couldn't load this view from Google right now.")
        st.caption(f"Facing {r['heading']}°" + (f" · taken {r['date']}" if r.get("date") else "")
                   + (" · _rejected, hidden from cards_" if rejected else ""))
        c1, c2, c3 = st.columns(3)
        c1.button("Turn left", icon=":material/rotate_left:", key="sv_left", disabled=rejected, width="stretch",
                  on_click=_sv_update, args=(shop,), kwargs={"turn": -30})
        c2.button("Turn right", icon=":material/rotate_right:", key="sv_right", disabled=rejected, width="stretch",
                  on_click=_sv_update, args=(shop,), kwargs={"turn": 30})
        if rejected:
            c3.button("Use it again", icon=":material/undo:", key="sv_undo", width="stretch",
                      on_click=_sv_update, args=(shop,), kwargs={"status": "OK"})
        else:
            c3.button("Reject", icon=":material/block:", key="sv_reject", width="stretch",
                      on_click=_sv_update, args=(shop,), kwargs={"status": "rejected"})


def review_page():
    st.title("🖼️ Review photos")
    st.caption("Pick what each photo shows. Changes save straight away and the shop cards update. "
               "Rejected photos are hidden from the cards. Photos are shown from the shop's own site, never copied.")
    rows = _photo_rows()
    names = ALL["name"].tolist()
    status = _photo_status(names, rows)
    n = {k: sum(v == k for v in status.values()) for k in ("storefront", "interior", "photos", "logo", "nothing")}
    st.markdown(f"**{n['storefront'] + n['interior']} of {len(names)} shops have a storefront or interior photo** "
                f"({n['storefront']} storefront, {n['interior']} interior only) · {n['photos']} product/other photos only · "
                f"{n['logo']} logo only · {n['nothing']} nothing")
    todo = [nm for nm in names if any(r["name"] == nm and r["photo_url"] and not r["type"] for r in rows)]
    no_front = [nm for nm in names if status[nm] != "storefront"]

    def label(nm):
        mark = "🟡 " if nm in todo else "✅ " if status[nm] == "storefront" else "▫️ "
        return f"{mark}{nm}"

    def next_shop(pool):
        cur = st.session_state.get("rv_shop")
        after = names[names.index(cur) + 1:] + names if cur in names else names
        st.session_state["rv_shop"] = next(nm for nm in after if nm in pool)

    c1, c2 = st.columns([3, 2], vertical_alignment="bottom")
    with c1:
        shop = st.selectbox("Shop", names, key="rv_shop", format_func=label)
    with c2:
        if todo:
            st.button(f"Next shop needing review ({len(todo)})", on_click=next_shop, args=(todo,), width="stretch",
                      icon=":material/skip_next:")
        else:
            st.button("Next shop without a storefront photo", on_click=next_shop, args=(no_front,), width="stretch",
                      icon=":material/skip_next:", disabled=not no_front)
    st.caption("🟡 has unreviewed photos · ✅ has a storefront photo · ▫️ no storefront photo yet")

    mine = sorted((r for r in rows if r["name"] == shop and r["photo_url"]),
                  key=lambda r: (r["type"] == "reject", _rank(r)))
    row = ALL[ALL["name"] == shop].iloc[0]
    own = [p for p in photos_for(row, False) if p["credit"] == "Your photo" or p["credit"].startswith("[Photo source]")]
    if own:
        st.caption(f"{len(own)} of your own photo(s) for this shop come from data/photos/ or the photo_urls column "
                   "and are always shown first; they're managed there, not here.")
    streetview_review(shop, status[shop] == "storefront")
    if not mine:
        st.info("No photos found online for this shop. Add your own in data/photos/"
                f"{slug(shop)}/ (see README), or a link in the photo_urls column.")
        return
    options = ["Storefront", "Interior", "Product", "Logo", "Other", "Reject"]
    for i in range(0, len(mine), 3):
        for col, r in zip(st.columns(3), mine[i:i + 3]):
            with col, st.container(border=True):
                is_main = _rank(r) == 1 and r["type"] not in ("reject", "logo")
                st.html(f"<img src='{html.escape(r['photo_url'], quote=True)}' alt='' loading='lazy' "
                        "style='width:100%;height:190px;object-fit:cover;border-radius:8px;display:block;"
                        f"background:#f1ede6{';opacity:.45' if r['type'] == 'reject' else ''}'>")
                host = urlparse(r["source_page"]).netloc.replace("www.", "")
                st.caption(("⭐ **Main photo** · " if is_main else "") + f"from [{host}]({r['source_page']})"
                           + (" · _hidden from cards_" if r["type"] == "reject" else "")
                           + (" · _not reviewed yet_" if not r["type"] else ""))
                key = f"rv_t_{abs(hash((shop, r['photo_url'])))}"
                st.session_state[key] = r["type"].title() if r["type"] else None
                st.segmented_control("What it shows", options, key=key, on_change=_set_type,
                                     args=(shop, r["photo_url"], key), label_visibility="collapsed")
                if r["type"] not in ("reject", "logo"):
                    st.button("Main photo" if is_main else "Make main photo", key=key + "_main", disabled=is_main,
                              on_click=_make_main, args=(shop, r["photo_url"]), width="stretch",
                              icon=":material/star:")


# On Streamlit Community Cloud the app's files are read-only and reset on restart, so the
# Review photos page (which edits data/*.csv) is only offered when running on your own computer.
ON_CLOUD = os.path.isdir("/mount/src")
PAGES = {
    "shops": st.Page(shops_page, title="Shops", icon="🎁", url_path="shops", default=True),
    "dining": st.Page(dining_page, title="Eat & Drink", icon="🍽️", url_path="eat-drink"),
    "holiday": st.Page(holiday_page, title="Holiday", icon="🎄", url_path="holiday"),
}
pages = list(PAGES.values())
if not ON_CLOUD:
    pages.append(st.Page(review_page, title="Review photos", icon="🖼️", url_path="review-photos"))
st.navigation(pages).run()
flush_js()
