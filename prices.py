"""Market prices (aion2.exchange public API) and item names/icons (NCSOFT gameconst)."""
from __future__ import annotations

import json
import logging
import os
import queue
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("prices")

EXCHANGE = "https://aion2.exchange/api"
GAMECONST = "https://tw.ncsoft.com/aion2/api/gameconst/item"
VERSION = "0.0.4"
PROJECT_URL = "https://github.com/ahmkhn/aion2-vault"   # lets the exchange's owner see where traffic comes from
UA = f"aion2-vault/{VERSION} (personal storage valuation{'; +' + PROJECT_URL if PROJECT_URL else ''})"

# Polite defaults for a tool many people run at once. aion2.exchange is a small
# community site that updates hourly, so polling faster than that gains nothing.
EXCHANGE_DELAY = 2.0          # seconds between any two requests to the exchange
GAMECONST_DELAY = 1.0         # seconds between NCSOFT item-name lookups
CATALOG_TTL = 60 * 60         # your server's price list
SPARK_TTL = 3 * 60 * 60       # 7-day trend lines (4-hour buckets)
REGION_TTL = 12 * 60 * 60     # region medians move slowly
REGION_MAX_PEERS = 8          # sample of other servers used for the region median
FAIL_BACKOFF = 30 * 60        # wait this long before retrying a failed trend/region fetch


def _ssl_context():
    """Verify HTTPS the way Windows itself does (like Edge), via truststore.

    Python's default check reads a snapshot of the Windows certificate store and can't
    fetch missing roots, so PCs without a newer root (e.g. Let's Encrypt's YE chain
    used by aion2.exchange) fail with CERTIFICATE_VERIFY_FAILED. Windows' own check
    downloads missing roots on demand and honours antivirus/proxy certificates.
    """
    import ssl
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        return ssl.create_default_context()


SSL_CTX = _ssl_context()


def friendly_error(e: Exception) -> str:
    import ssl
    text = str(e)
    inner = getattr(e, "reason", None)
    if (isinstance(e, ssl.SSLCertVerificationError) or isinstance(inner, ssl.SSLCertVerificationError)
            or "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text):
        return ("couldn't verify the site's security certificate. Check that your PC's date and time "
                "are correct, and that antivirus HTTPS scanning isn't blocking AION 2 Vault")
    return text


class _Http:
    """Polite JSON GETs: at least `delay` seconds apart, retries on 429/5xx."""

    def __init__(self, delay: float, user_agent: str | None = UA):
        self.delay = delay
        self.headers = {"Accept": "application/json"}
        if user_agent:  # None = urllib's generic default
            self.headers["User-Agent"] = user_agent
        self._last = 0.0
        self._lock = threading.Lock()
        self.count = 0   # requests sent since start (shown in the footer)

    def get(self, url: str, params: dict | None = None, retries: int = 3):
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        for attempt in range(retries + 1):
            with self._lock:
                wait = self._last + self.delay - time.time()
                if wait > 0:
                    time.sleep(wait)
                self._last = time.time()
                self.count += 1
            req = urllib.request.Request(url, headers=self.headers)
            try:
                # uncached pages on a server the exchange hasn't served recently can be slow
                with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                    ra = e.headers.get("Retry-After", "")
                    time.sleep(int(ra) if ra.isdigit() else 2 ** (attempt + 1))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt < retries:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


class PriceBook:
    def __init__(self, data_dir: str):
        self.dir = data_dir
        self.http = _Http(delay=EXCHANGE_DELAY)
        self.lock = threading.RLock()
        self.server_id: int | None = None
        self.catalog: dict[int, dict] = {}
        self.catalog_at = 0.0
        self.sparks: dict[int, tuple[float, list[float]]] = {}
        self.spark_partial_at = 0.0
        self.sparks_busy = False
        self.servers: list[dict] = []
        self.default_server: int | None = None
        self.known = set(_load(self._p("known_keys.json"), []))
        self.error: str | None = None
        self.loading: int | None = None   # server id whose catalog is being fetched
        # lowest listings for your items on the other servers of the same region
        self.region: dict[int, list[float]] = {}
        self.region_at = 0.0
        self.region_keys: set[int] = set()
        self.region_servers = 0
        self.region_progress: tuple[int, int] | None = None   # (done, total) while fetching
        self.region_partial_at = 0.0   # last lookup of items that were new since the full pass

    def _p(self, name):
        return os.path.join(self.dir, name)

    # -------------------------------------------------------------- servers
    def load_servers(self):
        cached = _load(self._p("servers.json"), None)
        if cached and time.time() - cached.get("_at", 0) < 86400:
            data = cached
        else:
            data = self.http.get(f"{EXCHANGE}/servers")
            data["_at"] = time.time()
            _save(self._p("servers.json"), data)
        with self.lock:
            self.servers = data.get("servers", [])
            self.default_server = data.get("default")

    # -------------------------------------------------------------- catalog
    def set_server(self, server_id: int):
        with self.lock:
            if server_id != self.server_id:
                self.server_id = server_id
                self.catalog, self.catalog_at, self.sparks = {}, 0.0, {}
                self.region, self.region_at, self.region_keys = {}, 0.0, set()
                self.region_progress = None
                self.region_partial_at = 0.0
                cached = _load(self._p(f"catalog_{server_id}.json"), None)
                if cached:
                    self.catalog = {int(k): v for k, v in cached["items"].items()}
                    self.catalog_at = cached["_at"]
                reg = _load(self._p(f"region_{server_id}.json"), None)
                if reg:
                    self.region = {int(k): v for k, v in reg["prices"].items()}
                    self.region_at, self.region_servers = reg["_at"], reg["servers"]
                    self.region_keys = set(reg["keys"])

    def catalog_stale(self) -> bool:
        return time.time() - self.catalog_at > CATALOG_TTL

    def mark_stale(self):
        """Force a refetch without blanking what is on screen meanwhile."""
        with self.lock:
            self.catalog_at = 0.0
            self.sparks = {k: (0.0, pts) for k, (_, pts) in self.sparks.items()}

    def refresh_catalog(self):
        sid = self.server_id
        self.loading = sid
        try:
            self._fetch_catalog(sid)
        finally:
            if self.loading == sid:
                self.loading = None

    def _fetch_catalog(self, sid):
        items: dict[int, dict] = {}
        offset = 0
        while True:
            page = self.http.get(f"{EXCHANGE}/items", {"server": sid, "limit": 1000,
                                                       "offset": offset, "sort": "key"})
            if sid != self.server_id:
                return  # user switched server mid-fetch
            for it in page.get("items", []):
                items[int(it["item_key"])] = it
            offset += len(page.get("items", []))
            if not page.get("items") or offset >= page.get("total", 0):
                break
        now = time.time()
        with self.lock:
            if sid != self.server_id:
                return
            self.catalog, self.catalog_at = items, now
            self.known |= set(items)
            self.error = None
        _save(self._p(f"catalog_{sid}.json"), {"_at": now, "items": items})
        _save(self._p("known_keys.json"), sorted(self.known))

    # ----------------------------------------------------------- sparklines
    def refresh_sparks(self, keys):
        self.sparks_busy = True     # shown as background activity in the UI
        try:
            self._refresh_sparks(keys)
        finally:
            self.sparks_busy = False

    def _refresh_sparks(self, keys):
        now = time.time()
        with self.lock:
            need = [k for k in keys if k in self.catalog and now - self.sparks.get(k, (0, None))[0] > SPARK_TTL]
            sid = self.server_id
            # items that just appeared (loot, purchases) are batched up for 10 minutes
            only_new = need and all(k not in self.sparks for k in need) and len(self.sparks) > 0
            if only_new and now - self.spark_partial_at < 600:
                return
            if need:
                self.spark_partial_at = now
        for i in range(0, len(need), 20):   # up to 20 of yours + decoys, 25 per request
            batch = need[i:i + 20]
            try:
                data = self.http.get(f"{EXCHANGE}/sparklines", {"server": sid, "window": "7d",
                                                                "keys": self._with_decoys(batch, 25)}, retries=1)
            except Exception as e:
                log.warning("sparklines unavailable (%s); retrying in %d min", e, FAIL_BACKOFF // 60)
                with self.lock:
                    for k in batch:  # keep old points, try again after the backoff
                        self.sparks[k] = (now - SPARK_TTL + FAIL_BACKOFF, self.sparks.get(k, (0, []))[1])
                continue
            got = {int(s["item_key"]): [p["price"] for p in s.get("points", []) if p.get("price") is not None]
                   for s in data.get("sparklines", [])}
            with self.lock:
                if sid != self.server_id:
                    return
                for k in batch:
                    self.sparks[k] = (now, got.get(k, []))

    def spark(self, key: int) -> list[float]:
        return self.sparks.get(key, (0, []))[1]

    def _with_decoys(self, batch: list[int], size: int) -> str:
        """Pad a key list with random market items and shuffle it, so a request doesn't
        reveal exactly which items you own. Costs no extra requests."""
        mine = set(batch)
        pool = [k for k in self.catalog if k not in mine]
        keys = list(batch) + random.sample(pool, max(0, min(size - len(batch), len(pool))))
        random.shuffle(keys)
        return ",".join(map(str, keys))

    # ------------------------------------------------------ region prices
    def region_peers(self) -> list[int]:
        me = next((s for s in self.servers if s["server_id"] == self.server_id), None)
        if not me or not me.get("region"):
            return []
        return [s["server_id"] for s in self.servers
                if s.get("region") == me["region"] and s["server_id"] != self.server_id
                and s.get("has_data", True)]

    def refresh_region(self, keys):
        """Lowest listing of each of your items on every other server in the region.

        One request per sampled server (100 items each). A full pass runs every
        REGION_TTL; items that are new since then are looked up at most once an hour.
        Used to catch prices that are anomalous on your server.
        """
        keys = sorted(k for k in keys if k in self.catalog)
        now = time.time()
        with self.lock:
            sid = self.server_id
            stale = now - self.region_at >= REGION_TTL
            missing = set(keys) - self.region_keys
            if not keys or (not stale and (not missing or now - self.region_partial_at < 3600)):
                return
            self.region_partial_at = now
        if not stale:
            keys = sorted(missing)
        peers = self.region_peers()
        if len(peers) > REGION_MAX_PEERS:  # a random sample spreads load across servers
            peers = random.sample(peers, REGION_MAX_PEERS)
        acc: dict[int, list[float]] = {}
        ok = 0
        for n, peer in enumerate(peers):
            self.region_progress = (n, len(peers))
            got = False
            for i in range(0, len(keys), 60):   # 60 of yours + 40 decoys per request
                batch = keys[i:i + 60]
                try:
                    data = self.http.get(f"{EXCHANGE}/items", {"server": peer, "limit": 100,
                                                               "keys": self._with_decoys(batch, 100)}, retries=0)
                except Exception as e:
                    log.debug("region %s: %s", peer, e)
                    continue
                if sid != self.server_id:
                    return
                got = True
                wanted = set(batch)
                for it in data.get("items", []):
                    if it.get("price") and int(it["item_key"]) in wanted:
                        acc.setdefault(int(it["item_key"]), []).append(float(it["price"]))
            ok += got
        self.region_progress = None
        now = time.time()
        if peers and ok < max(1, len(peers) // 3):
            # the exchange is struggling: keep what we had, try again after the backoff
            with self.lock:
                if sid == self.server_id and stale:
                    self.region_at = now - REGION_TTL + FAIL_BACKOFF
            log.warning("region prices: only %d/%d servers answered; keeping previous data", ok, len(peers))
            return
        with self.lock:
            if sid != self.server_id:
                return
            if stale:
                self.region, self.region_at = acc, now
                self.region_keys, self.region_servers = set(keys), ok
            else:  # merge the newly seen items into the existing pass
                self.region.update(acc)
                self.region_keys |= set(keys)
            snap = {"_at": self.region_at, "servers": self.region_servers,
                    "keys": sorted(self.region_keys),
                    "prices": {str(k): v for k, v in self.region.items()}}
        _save(self._p(f"region_{sid}.json"), snap)
        log.info("region prices: %d/%d servers answered", ok, len(peers))


class ItemInfo:
    """Name / icon / grade per item key from NCSOFT's public gameconst API, cached on disk."""

    def __init__(self, data_dir: str, bundled_path: str | None = None, lang: str = "en"):
        self.path = os.path.join(data_dir, "iteminfo.json")
        self.lang = lang
        # the tool's name and project URL only go to aion2.exchange, not to NCSOFT
        self.http = _Http(delay=GAMECONST_DELAY, user_agent=None)
        self.lock = threading.Lock()
        # names + local icons shipped with the release: no NCSOFT lookups for these
        bundled = _load(bundled_path, {}) if bundled_path else {}
        self.bundled: dict[int, dict] = {int(k): v for k, v in bundled.get("items", {}).items()}
        self.bundled_at = bundled.get("built")
        raw = _load(self.path, {})
        self.info: dict[int, dict] = {int(k): v for k, v in raw.items()}
        self.q: queue.Queue[int] = queue.Queue()
        self.queued: set[int] = set()
        threading.Thread(target=self._worker, name="iteminfo", daemon=True).start()

    def get(self, key: int) -> dict | None:
        b = self.bundled.get(key)
        return b if b and b.get("name") else self.info.get(key) or b

    def pending(self) -> set[int]:
        with self.lock:
            return set(self.queued)

    def want(self, keys):
        with self.lock:
            for k in keys:
                if k in self.bundled:
                    continue
                rec = self.info.get(k)
                fresh = rec and (rec.get("name") or time.time() - rec.get("_at", 0) < 6 * 3600)
                if not fresh and k not in self.queued:
                    self.queued.add(k)
                    self.q.put(k)

    def _worker(self):
        dirty = 0
        while True:
            try:
                k = self.q.get(timeout=2)
            except queue.Empty:
                if dirty:
                    self._flush()
                    dirty = 0
                continue
            try:
                d = self.http.get(GAMECONST, {"id": k, "enchantLevel": 0, "lang": self.lang}, retries=1)
                if str(d.get("id")) == str(k) and d.get("name"):
                    rec = {k2: d.get(k2) for k2 in ("name", "icon", "grade", "gradeName",
                                                      "categoryName", "tradable", "desc")}
                else:
                    rec = {"name": None}
            except Exception as e:
                log.debug("iteminfo %s: %s", k, e)
                rec = {"name": None}
            rec["_at"] = time.time()
            with self.lock:
                self.info[k] = rec
                self.queued.discard(k)
            dirty += 1
            if dirty >= 25:
                self._flush()
                dirty = 0

    def _flush(self):
        with self.lock:
            snap = dict(self.info)
        try:
            _save(self.path, snap)
        except Exception as e:
            log.warning("could not save item info: %s", e)
