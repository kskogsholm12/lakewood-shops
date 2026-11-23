"""Helpers for the "Plan a route" page: reading shop hours, walking distances, and putting stops
in a sensible walking order. No Streamlit in here, so it is easy to test on its own.

Hours are free text in data/businesses.csv, e.g. "Tue-Sat 12-6pm; Sun 12-5pm; Mon closed".
Split openings on one day use "&": "Mon-Sat 11:30am-2:30pm & 5-9:30pm".
parse_hours() understands that style. Anything it can't read is treated as "hours unknown".
"""
import datetime as dt
import math
import random
import re
from urllib.parse import quote_plus

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DAY = r"(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*"
_DAYSPEC = re.compile(rf"\b({_DAY}(?:\s*-\s*{_DAY})?(?:\s*(?:,|and|&)\s*{_DAY}(?:\s*-\s*{_DAY})?)*)\b", re.I)
_TIME = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?"
_RANGE = re.compile(rf"{_TIME}\s*-\s*{_TIME}", re.I)

WALK_MPH = 3.0       # an easy browsing pace
DETOUR = 1.25        # streets aren't straight lines; Lakewood's grid adds about a quarter


# ---------------------------------------------------------------- hours

def _days(spec: str) -> list:
    out = []
    for part in re.split(r"\s*(?:,|and|&)\s*", spec.lower()):
        ends = [p.strip()[:3] for p in part.split("-")]
        if not ends[0] or ends[0] not in DAYS:
            continue
        a = DAYS.index(ends[0])
        b = DAYS.index(ends[-1]) if ends[-1] in DAYS else a
        i = a
        while True:  # handles wrap-around like Sun-Mon
            out.append(i)
            if i == b:
                break
            i = (i + 1) % 7
    return out


def _mins(h, m, ampm) -> int:
    h, m = int(h), int(m or 0)
    if ampm and ampm.lower() == "pm" and h != 12:
        h += 12
    if ampm and ampm.lower() == "am" and h == 12:
        h = 0
    return h * 60 + m


def _range(m) -> tuple:
    h1, m1, ap1, h2, m2, ap2 = m.groups()
    end = _mins(h2, m2, ap2 or "pm")
    if ap1:
        start = _mins(h1, m1, ap1)
    else:  # "5-10pm" is 5pm; "11-7pm" is 11am; "12-6pm" is noon
        start = _mins(h1, m1, ap2 or "pm")
        if start > end:
            start = _mins(h1, m1, "am")
    if end <= start:  # closes after midnight
        end += 24 * 60
    return start, end


def parse_hours(text: str):
    """{weekday 0-6: [(open_min, close_min), ...]} or None if the hours are blank or unreadable.
    Days that aren't mentioned at all are taken as closed. "By appointment" days count as closed
    (you can't just walk in)."""
    if not text or not text.strip():
        return None
    week = {d: [] for d in range(7)}
    seen = False
    for seg in re.split(r"[;,](?![^()]*\))", text):
        seg = seg.strip()
        if not seg:
            continue
        dm = _DAYSPEC.search(seg)
        days = _days(dm.group(1)) if dm else []
        if not days:
            continue
        low = seg.lower()
        seen = True
        if "closed" in low or ("appointment" in low and not text.lower().startswith("by appointment")):
            continue
        # "Mon-Sat 11:30am-2:30pm & 5-9:30pm" gives a day two separate openings
        rngs = list(_RANGE.finditer(seg[dm.end():])) or list(_RANGE.finditer(seg))
        for rng in rngs:
            for d in days:
                week[d].append(_range(rng))
    if not seen:
        return None
    if text.lower().startswith("by appointment"):
        return {d: [] for d in range(7)}
    return week


def _fmt(mins: int) -> str:
    mins %= 24 * 60
    h, m = divmod(mins, 60)
    suffix = "am" if h < 12 else "pm"
    h = h % 12 or 12
    return f"{h}{':%02d' % m if m else ''}{suffix}"


def status_at(hours_text: str, when: dt.datetime) -> dict:
    """What a shop is doing at `when`: {"state": open|closed|unknown|appointment, "label": short text}."""
    if hours_text and hours_text.lower().startswith("by appointment"):
        return {"state": "appointment", "label": "By appointment only"}
    week = parse_hours(hours_text)
    if week is None:
        return {"state": "unknown", "label": "Hours not listed, check first"}
    d, t = when.weekday(), when.hour * 60 + when.minute
    for start, end in week[d]:
        if start <= t < end:
            return {"state": "open", "label": f"Open till {_fmt(end)}", "closes": end}
    for start, end in week[(d - 1) % 7]:  # still open from last night
        if end > 24 * 60 and t < end - 24 * 60:
            return {"state": "open", "label": f"Open till {_fmt(end)}", "closes": end - 24 * 60}
    later = sorted(s for s, _ in week[d] if s > t)
    if later:
        return {"state": "closed", "label": f"Closed then, opens {_fmt(later[0])}", "opens": later[0]}
    for k in range(1, 8):
        day = (d + k) % 7
        if week[day]:
            name = "tomorrow" if k == 1 else DAYS[day].title()
            return {"state": "closed", "label": f"Closed then, opens {name} {_fmt(min(s for s, _ in week[day]))}"}
    return {"state": "closed", "label": "Closed"}


def open_for_visit(hours_text: str, start: dt.datetime, window_min: int = 120, need_min: int = 30) -> str:
    """Can you shop here sometime in the next `window_min` minutes, for at least `need_min`?
    Returns "yes", "no" or "unknown"."""
    week = parse_hours(hours_text)
    if hours_text and hours_text.lower().startswith("by appointment"):
        return "no"
    if week is None:
        return "unknown"
    d, t = start.weekday(), start.hour * 60 + start.minute
    for s, e in week[d]:
        if max(s, t) + need_min <= min(e, t + window_min + need_min):
            return "yes"
    return "no"


# ---------------------------------------------------------------- distance and ordering

def miles(a, b) -> float:
    """Walking miles between two (lat, lon) points: straight line plus a bit for the street grid."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h)) * DETOUR


def walk_minutes(mi: float) -> int:
    return max(1, round(mi / WALK_MPH * 60)) if mi > 0.01 else 0


def path_length(pts: list, order: list) -> float:
    return sum(miles(pts[order[i]], pts[order[i + 1]]) for i in range(len(order) - 1))


def walking_order(pts: list) -> list:
    """Indexes of pts in a short walking order (an open path, not a loop). Tries every starting
    shop with nearest-neighbour and then tidies it up with 2-opt; plenty fast for a dozen stops."""
    n = len(pts)
    if n <= 2:
        return list(range(n))
    best = None
    for s in range(n):
        order, left = [s], set(range(n)) - {s}
        while left:
            nxt = min(left, key=lambda j: miles(pts[order[-1]], pts[j]))
            order.append(nxt)
            left.remove(nxt)
        improved = True
        while improved:
            improved = False
            for i in range(1, n - 1):
                for k in range(i + 1, n):
                    new = order[:i] + order[i:k + 1][::-1] + order[k + 1:]
                    if path_length(pts, new) + 1e-9 < path_length(pts, order):
                        order, improved = new, True
            # also try flipping the start segment (open path)
            for k in range(1, n):
                new = order[:k + 1][::-1] + order[k + 1:]
                if path_length(pts, new) + 1e-9 < path_length(pts, order):
                    order, improved = new, True
        if best is None or path_length(pts, order) < path_length(pts, best):
            best = order
    # Start from the west end so routes read the same way every time (Reverse flips it).
    if pts[best[0]][1] > pts[best[-1]][1]:
        best = best[::-1]
    return best


def pick_stops(cands: list, n: int, *, surprise: bool, seed=None) -> list:
    """Choose n nearby candidates for a walkable route. cands: [{"name", "category", "pt"}].
    Starts from the busiest cluster (or a random shop with surprise=True) and keeps adding the
    closest shop, nudging toward categories it doesn't have yet so the route is a mix."""
    if len(cands) <= n:
        return list(cands)
    rng = random.Random(seed)
    k = min(n, len(cands))

    def crowd(c):  # how close its k-1 nearest neighbours are
        return sum(sorted(miles(c["pt"], o["pt"]) for o in cands if o is not c)[:k - 1])

    start = rng.choice(cands) if surprise else min(cands, key=crowd)
    chosen, left = [start], [c for c in cands if c is not start]
    while len(chosen) < k:
        cats = {c["category"] for c in chosen}

        def cost(c):
            d = min(miles(c["pt"], s["pt"]) for s in chosen)
            d *= 1.8 if c["category"] in cats else 1.0
            return d * (rng.uniform(0.6, 1.4) if surprise else 1.0)

        nxt = min(left, key=cost)
        chosen.append(nxt)
        left.remove(nxt)
    return chosen


# ---------------------------------------------------------------- map links

def _place(name: str, address: str) -> str:
    return f"{name}, {address}" if address else name


def google_route_links(stops: list, per_link: int = 10) -> list:
    """Google Maps walking-directions links for stops [(name, address)], in order. Google takes up
    to 10 places per link (start + 8 waypoints + end), so longer routes are split into legs that
    share their end stop. Returns [(label, url)]."""
    out, i = [], 0
    while i < len(stops) - 1 or (i == 0 and len(stops) == 1):
        leg = stops[i:i + per_link]
        if len(leg) == 1:
            url = ("https://www.google.com/maps/dir/?api=1&travelmode=walking&destination="
                   + quote_plus(_place(*leg[0])))
        else:
            url = ("https://www.google.com/maps/dir/?api=1&travelmode=walking"
                   f"&origin={quote_plus(_place(*leg[0]))}&destination={quote_plus(_place(*leg[-1]))}")
            if len(leg) > 2:
                url += "&waypoints=" + quote_plus("|".join(_place(*s) for s in leg[1:-1]))
        out.append((f"stops {i + 1}-{i + len(leg)}", url))
        i += per_link - 1
        if len(leg) == 1:
            break
    return out


def walk_here_links(name: str, address: str) -> tuple:
    """(Google Maps, Apple Maps) walking directions from wherever you are now to one shop."""
    q = quote_plus(_place(name, address))
    return (f"https://www.google.com/maps/dir/?api=1&travelmode=walking&destination={q}",
            f"https://maps.apple.com/?daddr={q}&dirflg=w")


def small_business_saturday(year: int) -> dt.date:
    """The Saturday after US Thanksgiving (4th Thursday of November)."""
    nov1 = dt.date(year, 11, 1)
    thanksgiving = nov1 + dt.timedelta(days=(3 - nov1.weekday()) % 7 + 21)
    return thanksgiving + dt.timedelta(days=2)
