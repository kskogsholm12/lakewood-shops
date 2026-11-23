# Shop Local Lakewood

A searchable catalog of small, locally owned businesses in Lakewood, Ohio, built with Streamlit.

## Run it

```bash
pip install -r requirements.txt
streamlit run app.py
```

It opens at http://localhost:8501.

## Editing the data

Everything is in `data/businesses.csv`. Open it in Excel, Google Sheets or a text editor.

- **Add a business:** add a row. Only `name` is required.
- **Add a new field** (e.g. `owner`, `price_range`, `gift_cards`): add a column. It shows up on the cards and in the table automatically.
- `source_urls`: where the info came from, separated by ` ; `.
- `status`: `listed`, `online only`, or `needs check` if the only sources are old or conflicting.
- `instagram`, `gift_cards` (yes/blank): filled where the shop lists them.
- `last_checked`: the date the row was last reviewed.

Hours, phone and Instagram were filled from each shop's own website where it listed them. Restaurant info comes from Cleveland Magazine's Sept 2026 dining guide. Rows marked `needs check` have only old or conflicting sources.

## Map

The Map tab places each shop by its address. The first time it opens, `geocode.py` looks addresses up with the free US Census geocoder (OpenStreetMap as a fallback) and saves them in `data/geocache.csv`, so later loads are instant. New addresses are looked up automatically. Delete that file to redo all lookups.

## Photos

Each card opens with a small **slideshow** (storefront first, then interior, then product shots)
with the shop's **logo as a round badge** in the corner. Use the arrows or dots, or swipe on a
phone. Logos are never a slide; if a shop has only a logo, the card shows the logo on a plain
background until a real photo is found.

Click anywhere on a card (or **See more**) to expand it to fill the page: a bigger slideshow with
thumbnails, every detail, a small map, and **Back to all shops** to return to where you were. The
open shop is in the address bar (`?shop=apple-jax-toys`), so the browser's Back button closes it
and you can bookmark or share a shop. There are no pop-ups anywhere in the app, by design.

Every photo has a **type**: `storefront`, `interior`, `product`, `logo`, or `other`
(`reject` hides a photo). Photos come from three places, yours first:

1. **Your own photos.** Put files in `data/photos/<shop-name>/` (lowercase, dashes, e.g.
   `data/photos/apple-jax-toys/`). Start the file name with the type:
   `storefront-1.jpg`, `interior-1.jpg`, `interior-2.jpg`, `logo.png`.
2. **Photo links you add** in the `photo_urls` column, as `type: URL`, separated by ` ; `,
   e.g. `storefront: https://... ; interior: https://...`.
3. **Photos found online**, in `data/photos.csv` (columns: name, photo_url, source_page, kind,
   type, rank). `fetch_photos.py` gathers candidates from the shop's website and single-shop
   articles and detects logos. A candidate with an empty `type` hasn't been looked at yet; a
   review pass (by you or Claude) sets each type, or `reject`. Only links are stored; images load
   from the original site and are credited under the photo.

Shops with no photo and no logo show a placeholder icon for their category.

## Google Street View storefronts

For shops with no storefront photo, the app can show a Google Street View image of the shop front.

1. Put your key in `.streamlit/secrets.toml` (this file is in `.gitignore`):
   `GOOGLE_MAPS_API_KEY = "..."`
2. Run `.venv/bin/python streetview.py` once. It uses Google's free Street View *metadata* endpoint to find
   the nearest outdoor panorama for each shop and the compass heading that faces the shop, and saves
   `data/streetview.csv` (no images, no key). `--all` redoes every shop; views you rejected stay rejected.
3. The app fetches each image on the server (640x400, cached for a day) and sends it to the page as an image,
   so the key never reaches the browser. It is credited "© Google Street View (date)" and comes after any
   real storefront photo but before interior photos. On the Review photos page you can turn the camera
   30° left or right, or reject the view.

Without a key, Street View is skipped and the app works as before.

## Plan a route

The **Plan a route** page helps you explore shops you haven't been to yet, on foot.

- **When are you going?** Pick a day (🛍️ marks Small Business Weekend) and a start time. Every stop shows
  when you'd get there and whether it's open then, read from the `hours` column. Hours the app can't read,
  or blank hours, show as "check first".
- **Let the app pick:** **Surprise me** picks a different walkable cluster each tap; **Closest together**
  picks the tightest one. Choose Madison, Detroit or anywhere, how many stops, and (under More choices)
  kinds of places, a coffee/food stop, and whether to skip shops that are closed or have no listed hours.
  Salons and restaurants are left out of picks unless you choose them (one food stop is added if you want).
- **Add to route** is on every shop card too. Stops are put in walking order automatically; you can move
  them, reverse, remove, or undo.
- **Open walking directions in Google Maps** opens the whole route (Google takes 10 stops per link, so longer
  routes get one button per leg). Each stop also has Google and Apple Maps links to walk there from where you are.
- The route is kept in the page address (`/route?stops=...`), so a bookmark keeps it.

Distances are straight lines plus 25% for the street grid, at 3 mph. The route logic is in `route.py`.

## Holiday info

The **Holiday** page lists upcoming holiday events and Small Business Weekend deals from `data/holiday.csv`.
To add one, add a row with these columns:

| column | what goes in it |
|---|---|
| `kind` | `event` (shown under "Coming up") or `deal` (shown under "Small Business Weekend deals") |
| `title` | short name, e.g. `Light Up Lakewood` |
| `business` | the shop's name exactly as in `businesses.csv` (or blank). A matching shop gets a 🎄 line on its card, and deals link to its card |
| `start`, `end` | dates as `YYYY-MM-DD` (leave `end` blank for a one-day event) |
| `time` | free text, e.g. `4-8pm` or `Fri 6-10pm, Sat 10am-6pm` |
| `place`, `address` | where it is; the address becomes a Google Maps link |
| `details` | one or two sentences |
| `source` | link to where the info came from |

Rows hide themselves once their end date has passed (Lakewood time), so there's no need to delete old ones.
Anything happening today or this weekend gets a light highlight.
