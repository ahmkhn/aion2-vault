"""Build the bundled item database: names/details + 64 px icons for every market item.

    python tools/build_itemdb.py            incremental: only fetch items not built yet
    python tools/build_itemdb.py --full     refetch everything

Writes web/itemdb.json and web/icons/*.webp. The bundled app then never asks NCSOFT
about the items you own. Progress is cached in tools/.itemdb_cache.json, so an
interrupted run resumes where it stopped. Needs Pillow (build-time only).
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from prices import EXCHANGE, GAMECONST, UA, _Http  # noqa: E402

WEB = os.path.join(ROOT, "web")
ICONS = os.path.join(WEB, "icons")
OUT = os.path.join(WEB, "itemdb.json")
CACHE = os.path.join(HERE, ".itemdb_cache.json")
ICON_PX = 64
ICON_SITE = "https://aion2.exchange/item-icons"   # fallback icons for items NCSOFT doesn't list
# item keys are global but each server lists a different subset, so sample
# one Elyos and one Asmodian server in every region (plus a few known busy ones)
EXTRA_SERVERS = (11304, 12301, 12101, 11101)
FIELDS = ("name", "grade", "gradeName", "categoryName", "tradable", "desc")


def catalog_servers(http: _Http) -> list[int]:
    servers = http.get(f"{EXCHANGE}/servers").get("servers", [])
    picked, seen = list(EXTRA_SERVERS), set()
    for s in servers:
        group = (s.get("region"), s.get("faction"))
        if group not in seen and s.get("has_data", True):
            seen.add(group)
            if s["server_id"] not in picked:
                picked.append(s["server_id"])
    return picked


def market_keys(http: _Http, names: dict[int, dict] | None = None) -> set[int]:
    # items the local app has already seen on any server it was pointed at
    keys: set[int] = set(_read(os.path.join(ROOT, "data", "known_keys.json"), []))
    for sid in catalog_servers(http):
        offset = 0
        while True:
            page = http.get(f"{EXCHANGE}/items", {"server": sid, "limit": 1000, "offset": offset, "sort": "key"})
            items = page.get("items", [])
            keys |= {int(i["item_key"]) for i in items}
            if names is not None:   # market name + category, for items NCSOFT doesn't know
                for i in items:
                    names.setdefault(int(i["item_key"]), {"name": i.get("name"),
                                                          "categoryName": (i.get("category") or {}).get("name")})
            offset += len(items)
            if not items or offset >= page.get("total", 0):
                break
        print(f"  catalog {sid}: {len(keys)} unique items so far", flush=True)
    return keys


def fetch_icon(url: str, dest: str, user_agent: str | None = None):
    from PIL import Image
    headers = {"Accept": "image/*", **({"User-Agent": user_agent} if user_agent else {})}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        img = Image.open(io.BytesIO(r.read())).convert("RGBA")
    img.thumbnail((ICON_PX, ICON_PX), Image.LANCZOS)
    img.save(dest, "WEBP", quality=82, method=6)


def save_cache(cache):
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    os.replace(tmp, CACHE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="refetch every item")
    ap.add_argument("--fill-missing", action="store_true",
                    help="only fill icons for items already in the database that have none, using the "
                         "catalogs saved in data/ (no catalog crawl, no NCSOFT requests)")
    args = ap.parse_args()
    os.makedirs(ICONS, exist_ok=True)
    cache = {} if args.full else _read(CACHE, {})

    exchange = _Http(delay=2.0, user_agent=UA)
    ncsoft = _Http(delay=1.0, user_agent=None)
    market_names: dict[int, dict] = {}
    if args.fill_missing:
        keys = sorted(int(k) for k in cache)
        for f in os.listdir(os.path.join(ROOT, "data")):
            if f.startswith("catalog_") and f.endswith(".json"):
                for k, it in _read(os.path.join(ROOT, "data", f), {}).get("items", {}).items():
                    market_names.setdefault(int(k), {"name": it.get("name"),
                                                     "categoryName": (it.get("category") or {}).get("name")})
        todo = []
    else:
        print("Collecting market item keys…", flush=True)
        keys = sorted(market_keys(exchange, market_names))
        todo = [k for k in keys if str(k) not in cache]
    print(f"{len(keys)} market items, {len(todo)} to fetch (~{len(todo) // 60 + 1} min)", flush=True)

    t0 = time.time()
    for n, k in enumerate(todo, 1):
        try:
            d = ncsoft.get(GAMECONST, {"id": k, "enchantLevel": 0, "lang": "en"}, retries=2)
        except Exception as e:
            print(f"  {k}: {e}", flush=True)
            continue
        rec = {f: d.get(f) for f in FIELDS} if str(d.get("id")) == str(k) and d.get("name") else {"name": None}
        if d.get("icon") and rec.get("name"):
            fname = hashlib.sha1(d["icon"].encode()).hexdigest()[:16] + ".webp"
            path = os.path.join(ICONS, fname)
            if not os.path.exists(path):
                try:
                    fetch_icon(d["icon"], path)
                    time.sleep(0.3)
                except Exception as e:
                    print(f"  icon {k}: {e}", flush=True)
                    fname = None
            rec["icon"] = f"icons/{fname}" if fname else None
        cache[str(k)] = rec
        if n % 25 == 0 or n == len(todo):
            save_cache(cache)
            rate = n / max(1e-6, time.time() - t0)
            print(f"  {n}/{len(todo)}  (~{(len(todo) - n) / rate / 60:.1f} min left)", flush=True)

    fill_from_exchange(cache, keys, market_names)

    # items nobody has an entry for are kept as {"name": null}, so the app knows
    # not to ask about them at runtime either
    db = {k: v for k, v in cache.items() if int(k) in set(keys) or v.get("name")}
    used = {os.path.basename(v["icon"]) for v in db.values() if v.get("icon")}
    for f in os.listdir(ICONS):            # drop icons no item uses any more
        if f not in used:
            os.remove(os.path.join(ICONS, f))
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"built": int(time.time()), "items": db}, f, separators=(",", ":"))
    size = sum(os.path.getsize(os.path.join(ICONS, f)) for f in os.listdir(ICONS))
    named = sum(1 for v in db.values() if v.get("name"))
    print(f"Done: {named} named items (+{len(db) - named} with no NCSOFT entry), {len(used)} icons "
          f"({size / 1024 / 1024:.1f} MB), {OUT}", flush=True)


def fill_from_exchange(cache: dict, keys, market_names: dict[int, dict]):
    """Some market items (e.g. Balaur's Essence, Sealed Kromede Wings) aren't in NCSOFT's
    item API at all. aion2.exchange keeps its own icon set for every market item: use its
    index for those, with the market name and category. Only the missing items are fetched."""
    missing = [k for k in keys if not (cache.get(str(k)) or {}).get("icon")]
    if not missing:
        return
    exchange = _Http(delay=2.0, user_agent=UA)
    try:
        index = exchange.get(f"{ICON_SITE}/index.json")
    except Exception as e:
        print(f"  aion2.exchange icon index unavailable: {e}", flush=True)
        return
    files, items, rarities = index.get("files", []), index.get("items", {}), index.get("rarities", [])
    added = 0
    for k in missing:
        entry, info = items.get(str(k)), market_names.get(k) or {}
        if not entry or not info.get("name"):
            continue
        fi, ri = entry[0], entry[1] if len(entry) > 1 else None
        if not isinstance(fi, int) or not 0 <= fi < len(files) or not re.fullmatch(r"[0-9a-f]{12}", str(files[fi])):
            continue
        # same 16-hex naming as NCSOFT icons: the app only serves /icons/<16 hex>.webp
        fname = hashlib.sha1(f"aion2.exchange:{files[fi]}".encode()).hexdigest()[:16] + ".webp"
        path = os.path.join(ICONS, fname)
        if not os.path.exists(path):
            try:
                fetch_icon(f"{ICON_SITE}/{files[fi]}.webp", path, UA)
                time.sleep(2.0)   # same pace as the app's other aion2.exchange requests
            except Exception as e:
                print(f"  icon {k}: {e}", flush=True)
                continue
        rec = dict(cache.get(str(k)) or {})
        grade = rarities[ri]["name"] if isinstance(ri, int) and 0 <= ri < len(rarities) else None
        rec.update({"name": rec.get("name") or info["name"], "icon": f"icons/{fname}",
                    "categoryName": rec.get("categoryName") or info.get("categoryName"),
                    "grade": rec.get("grade") or grade, "gradeName": rec.get("gradeName") or grade,
                    "tradable": True, "source": "aion2.exchange"})
        cache[str(k)] = rec
        added += 1
    save_cache(cache)
    print(f"  {added} of {len(missing)} items without an NCSOFT icon filled from aion2.exchange", flush=True)


def _read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


if __name__ == "__main__":
    main()
