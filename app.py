"""AION 2 Vault — see what your inventory and storage are worth on the market.

Usage:
    python app.py              app window (start this BEFORE logging in to a character)
    python app.py --browser    serve the UI in your browser instead of a window
    python app.py --demo       sample inventory, no capture
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import secrets
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import npcap
from prices import CATALOG_TTL, VERSION, ItemInfo, PriceBook, _load, _save, friendly_error
from protocol import CONTAINERS, COUNTED, HIDDEN, KINAH_KEY, Inventory

FROZEN = getattr(sys, "frozen", False)
HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(HERE, "web")
# installed builds keep caches/settings in %APPDATA%; a source checkout keeps them next to the code
DATA = (os.path.join(os.environ.get("APPDATA", HERE), "AION2Vault") if FROZEN
        else os.path.join(HERE, "data"))
TOKEN = secrets.token_urlsafe(16)   # required on POSTs so other web pages can't drive the app
ICON_PATH = re.compile(r"^/icons/[0-9a-f]{16}\.webp$")   # bundled item icons
log = logging.getLogger("app")

DEFAULT_SETTINGS = {"server_id": None, "server_chosen": False, "fee_pct": 0.0,
                    "price_mode": "fair", "include_equipped": False}
PRICE_MODES = ("fair", "lowest", "sold_avg")
OUTLIER_X = 2.5        # local price this many times the region median counts as an outlier
MIN_SALES = 3          # 24h sales needed before the sold average is trusted
MIN_REGION = 3         # other servers that must list an item for a region median


def _median(xs):
    xs = sorted(x for x in xs if x)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def price_for(m: dict, spark: list[float], region: list[float], mode: str) -> dict:
    """Unit price for one item plus the signals behind it.

    fair = the lowest of (current lowest listing, 7-day median of the lowest listing,
    24h average sale price when there were enough sales) — so a single overpriced
    listing can't inflate the value — then capped at the region median when it is
    still more than OUTLIER_X times what the other servers in the region ask.

    An item that isn't listed on your server now and hasn't sold recently has no price:
    old listings that never sold are only someone's asking price (one unsold Earth Bow
    listed at 500K for two days made a 500K "7-day median"). The region median still fills
    in when at least MIN_REGION other servers list it right now.
    """
    listing = m.get("price") or None
    sold_n = m.get("units_sold") or 0
    sold = m.get("sold_avg_price") if sold_n >= MIN_SALES else None
    # the 7-day median only pulls a current listing down; it never prices an item on its own
    week = _median(spark) if len(spark) >= 6 and listing else None
    reg = _median(region) if len(region) >= MIN_REGION else None
    basis = None
    if mode == "lowest":
        unit, basis = listing, "listing"
    elif mode == "sold_avg":
        unit, basis = (m.get("sold_avg_price"), "sold") if m.get("sold_avg_price") else (listing, "listing")
    else:
        cands = [(v, b) for v, b in ((listing, "listing"), (week, "week"), (sold, "sold")) if v]
        unit, basis = min(cands) if cands else (None, None)
        if reg:
            if unit is None:
                unit, basis = reg, "region"
            elif unit > OUTLIER_X * reg:
                unit, basis = reg, "region_cap"
    if unit is None:
        basis = "stale" if m.get("last_price") else None
    return {"unit": unit, "basis": basis, "p_listing": listing, "p_week": week,
            "p_sold": m.get("sold_avg_price"), "p_sold_n": sold_n,
            "p_region": reg, "p_region_n": len(region),
            # shown, never used: the last listing seen on this server and when
            "p_last": None if listing else m.get("last_price"),
            "last_seen": None if listing else m.get("last_seen_at")}


class Vault:
    def __init__(self, demo: bool, diagnose: bool = False):
        os.makedirs(DATA, exist_ok=True)
        self.demo = demo
        self.diagnose = diagnose
        self.lock = threading.RLock()
        self.inv = Inventory()
        self.prices = PriceBook(DATA)
        self.info = ItemInfo(DATA, os.path.join(WEB, "itemdb.json"))
        saved = _load(os.path.join(DATA, "settings.json"), {})
        self.settings = {**DEFAULT_SETTINGS, **saved}
        if saved.get("server_id") and "server_chosen" not in saved:
            self.settings["server_chosen"] = True   # settings from before this flag existed
        self.capture = None
        self.snapshot = _load(os.path.join(DATA, "snapshot.json"), None)
        self._dirty = False
        self._wake = threading.Event()
        self._last_refresh = 0.0

    # ------------------------------------------------------------- startup
    def start(self):
        try:
            self.prices.load_servers()
        except Exception as e:
            self.prices.error = f"Could not reach aion2.exchange: {friendly_error(e)}"
        sid = self.settings["server_id"] or self.prices.default_server or 11304
        self.settings["server_id"] = sid
        self.prices.set_server(sid)
        if not self.demo:
            from capture import GameCapture
            # bundled items count as known too, so a login can be read before prices arrive
            self.capture = GameCapture(self.inv, self.lock,
                                       lambda: self.prices.known | self.info.bundled.keys(),
                                       self._mark_dirty,
                                       diag_dir=os.path.join(DATA, "diagnostics") if self.diagnose else None)
            self.capture.start()
        threading.Thread(target=self._background, name="refresh", daemon=True).start()
        threading.Thread(target=self._region_loop, name="region", daemon=True).start()

    def _mark_dirty(self):
        self._dirty = True

    def _background(self):
        while True:
            delay = 5
            try:
                if self.prices.catalog_stale():
                    self.prices.refresh_catalog()
                    self.prices.error = None
                    if self.capture:
                        self.capture.flush_backlog()
            except Exception as e:
                self.prices.error = f"Price refresh failed: {friendly_error(e)}. Retrying in 5 minutes"
                log.warning("catalog refresh: %s", e)
                self.prices.catalog_at = time.time() - CATALOG_TTL + 300 if self.prices.catalog else 0
                delay = 300 if not self.prices.catalog else 5
            try:
                if self.demo and not self.inv.stacks and self.prices.catalog:
                    self._make_demo()
                keys = {key for (key, _cont), _n in self._totals()}
                # only market items are looked up (and almost all are bundled), so bound
                # or untradeable things in your bags never cause a request to NCSOFT
                self.info.want(k for k in keys if k in self.prices.catalog)
                self.prices.refresh_sparks(keys)   # failures back off internally
                if self._dirty and self.inv.loaded_at and not self.demo:
                    self._dirty = False
                    self._save_snapshot()
            except Exception as e:
                log.warning("refresh: %s", e)
                delay = 300
            self._wake.wait(delay)
            self._wake.clear()

    def _region_loop(self):
        """Region medians take minutes to gather, so they run apart from the main refresh."""
        while True:
            try:
                if self.prices.catalog:
                    self.prices.refresh_region({key for (key, _c), _n in self._totals()})
            except Exception as e:
                log.warning("region refresh: %s", e)
            time.sleep(30)

    def _make_demo(self):
        rng = random.Random(7)
        listed = [it for it in self.prices.catalog.values() if it.get("price")]
        for n, it in enumerate(rng.sample(listed, min(60, len(listed)))):
            price = it["price"]
            gear = (it.get("category") or {}).get("section_id") in ("weapons", "armor", "accessories", "skins")
            count = 1 if gear or price > 5e6 else rng.choice([1, 2, 5, 12, 40, 99, 250])
            sid = n.to_bytes(8, "little")
            self.inv.stacks[sid] = {"key": int(it["item_key"]), "count": count,
                                    "container": rng.choice([1, 2, 2, 15, 15])}
        self.inv.stacks[b"kinah\0\0\0"] = {"key": KINAH_KEY, "count": 4_250_000, "container": 1}
        self.inv.loaded_at = self.inv.updated_at = time.time()

    def _save_snapshot(self):
        with self.lock:
            totals = [[k, c, n] for (k, c), n in self.inv.totals().items()]
        self.snapshot = {"at": time.time(), "totals": totals}
        _save(os.path.join(DATA, "snapshot.json"), self.snapshot)

    def _totals(self) -> list[tuple[tuple[int, int], int]]:
        with self.lock:
            if self.inv.loaded_at:
                return list(self.inv.totals().items())
        if self.snapshot:
            return [((k, c), n) for k, c, n in self.snapshot["totals"]]
        return []

    # --------------------------------------------------------------- state
    def state(self) -> dict:
        s = self.settings
        fee = max(0.0, min(100.0, float(s["fee_pct"]))) / 100
        mode = s["price_mode"]
        rows = []
        kinah = 0
        kinah_by: dict[str, int] = {}   # Kinah is carried in the inventory and kept in server storage
        info_pending = self.info.pending()
        market = self.prices.known | self.info.bundled.keys()
        for (key, cont), count in self._totals():
            if key == KINAH_KEY:  # currency, shown on its own rather than as an item
                kinah += count
                where = CONTAINERS.get(cont, "Other")
                kinah_by[where] = kinah_by.get(where, 0) + count
                continue
            if cont == 11 and not s["include_equipped"]:
                continue
            if cont in HIDDEN:
                continue
            if key not in market:   # bound items have their own, never-listed IDs
                continue
            m = self.prices.catalog.get(key) or {}
            inf = self.info.get(key) or {}
            spark = self.prices.spark(key)
            pr = price_for(m, spark, self.prices.region.get(key, []), mode) if m else {"unit": None}
            unit = pr["unit"]
            cat = m.get("category") or {}
            if m:
                section = cat.get("section_name") or "Other"
            elif inf.get("tradable") is False:
                section = "Untradeable"
            else:
                section = "Not on market"
            net = unit * (1 - fee) if unit else None
            rows.append({
                **{k: (v * (1 - fee) if k.startswith("p_") and not k.endswith("_n") and v else v)
                   for k, v in pr.items() if k != "unit"},
                "key": key,
                "name": m.get("name") or inf.get("name") or f"Item #{key}",
                "icon": inf.get("icon"),
                "grade": inf.get("gradeName"),
                "desc": inf.get("desc"),
                "section": section,
                "category": cat.get("name") or inf.get("categoryName"),
                # unknown containers show as Other; only inventory and the two storages
                # count toward the totals (the Soul Sync recovery list is skipped above)
                "location": CONTAINERS.get(cont, "Other"),
                "container": cont,
                "counted": cont in COUNTED,
                "count": count,
                "unit": net,
                "total": net * count if net else None,
                "change": m.get("change_pct"),
                "listings": m.get("listings"),
                "sold": m.get("units_sold"),
                "spark": spark,
                "info_pending": key in info_pending,
            })
        live = self.inv.loaded_at is not None
        if self.demo:
            status = {"state": "demo", "text": "Demo inventory"}
        elif self.capture:
            status = self.capture.status()
            if not live and self.snapshot:
                status["text"] += " · showing last snapshot from " + \
                    time.strftime("%b %d %H:%M", time.localtime(self.snapshot["at"]))
        else:
            status = {"state": "idle", "text": "Capture disabled"}
        return {
            "status": status,
            "price_error": self.prices.error,
            "prices_at": self.prices.catalog_at or None,
            "catalog_size": len(self.prices.catalog),
            "region_servers": self.prices.region_servers,
            "region_at": self.prices.region_at or None,
            "region_progress": self.prices.region_progress,
            "refreshing": self.prices.loading is not None,
            "refresh_cooldown": self.refresh_cooldown(),
            "api_requests": {"exchange": self.prices.http.count, "ncsoft": self.info.http.count},
            "activity": self._activity(info_pending),
            "npcap_url": npcap.NPCAP_URL,
            "version": VERSION,
            "loading": bool(self.prices.loading) or not self.prices.catalog,
            "settings": s,
            "servers": self.prices.servers,
            "updated_at": self.inv.updated_at or (self.snapshot or {}).get("at"),
            "stats": self.capture.stats if self.capture else None,
            "kinah": kinah,
            "kinah_by": kinah_by,
            "items": rows,
        }

    def _activity(self, info_pending: set) -> list[str]:
        """What the app is fetching right now, for the small spinner in the header."""
        p, out = self.prices, []
        if p.loading or not p.catalog:
            out.append("Loading market prices")
        if p.sparks_busy:
            out.append("Loading 7-day trends")
        if p.region_progress:
            n, total = p.region_progress
            out.append(f"Checking region prices {n + 1}/{total}")
        if info_pending:
            out.append(f"Fetching item names & icons · {len(info_pending)} left")
        return out

    def update_settings(self, patch: dict):
        s = self.settings
        if "server_id" in patch:
            s["server_id"] = int(patch["server_id"])
            s["server_chosen"] = True
            self.prices.set_server(s["server_id"])
            self.prices.error = None
        if "fee_pct" in patch:
            s["fee_pct"] = float(patch["fee_pct"])
        if patch.get("price_mode") in PRICE_MODES:
            s["price_mode"] = patch["price_mode"]
        if "include_equipped" in patch:
            s["include_equipped"] = bool(patch["include_equipped"])
        _save(os.path.join(DATA, "settings.json"), s)
        self._wake.set()

    REFRESH_COOLDOWN = 30
    FRESH_ENOUGH = 10 * 60   # manual refresh is a no-op when prices are newer than this

    def refresh_cooldown(self) -> float:
        return max(0.0, self._last_refresh + self.REFRESH_COOLDOWN - time.time())

    def force_refresh(self) -> dict:
        left = self.refresh_cooldown()
        if left > 0:
            return {"ok": False, "cooldown": left}
        self._last_refresh = time.time()
        if self.prices.catalog and time.time() - self.prices.catalog_at < self.FRESH_ENOUGH:
            # the exchange only updates hourly; refetching recent data just adds load
            return {"ok": True, "fresh": True, "cooldown": float(self.REFRESH_COOLDOWN)}
        self.prices.mark_stale()
        self.prices.error = None
        self._wake.set()
        return {"ok": True, "cooldown": float(self.REFRESH_COOLDOWN)}


def make_handler(vault: Vault):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self):
            # blocks DNS-rebinding: only answer requests addressed to this machine
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if host in ("127.0.0.1", "localhost"):
                return True
            self._json({"error": "forbidden"}, 403)
            return False

        def do_GET(self):
            if not self._host_ok():
                return
            if self.path in ("/", "/index.html"):
                with open(os.path.join(WEB, "index.html"), "rb") as f:
                    body = f.read().replace(b"__VAULT_TOKEN__", TOKEN.encode())
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif ICON_PATH.match(self.path):
                path = os.path.join(WEB, "icons", self.path.rsplit("/", 1)[1])
                try:
                    with open(path, "rb") as f:
                        body = f.read()
                except OSError:
                    return self._json({"error": "not found"}, 404)
                self.send_response(200)
                self.send_header("Content-Type", "image/webp")
                self.send_header("Cache-Control", "max-age=604800")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/state":
                if not secrets.compare_digest(self.headers.get("X-Vault-Token", ""), TOKEN):
                    return self._json({"error": "forbidden"}, 403)
                self._json(vault.state())
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            if not self._host_ok():
                return
            if not secrets.compare_digest(self.headers.get("X-Vault-Token", ""), TOKEN):
                return self._json({"error": "forbidden"}, 403)
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            if self.path == "/api/settings":
                vault.update_settings(body)
                self._json({"ok": True})
            elif self.path == "/api/refresh":
                self._json(vault.force_refresh())
            elif self.path == "/api/npcap/recheck":
                if vault.capture:
                    vault.capture.recheck()
                self._json({"ok": True})
            elif self.path == "/api/restart-admin":
                ok = npcap.restart_as_admin()
                self._json({"ok": ok})
                if ok:  # the elevated copy takes over
                    threading.Timer(0.5, lambda: os._exit(0)).start()
            else:
                self._json({"error": "not found"}, 404)

    return H


def _free_port(preferred: int) -> int:
    for port in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise OSError("no free port")


def _single_instance() -> bool:
    """False if another AION 2 Vault is already running (named mutex)."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        _single_instance.handle = k32.CreateMutexW(None, False, "Local\\AION2Vault")
        return k32.GetLastError() != 183   # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def _unblock_bundle():
    """Remove the "downloaded from the internet" mark from the app's own bundled files.

    Windows tags every file unzipped from a browser download (Zone.Identifier), and
    .NET Framework then refuses to load the bundled DLLs the app window needs. This is
    what right-click > Properties > Unblock does, limited to the app's own folder.
    """
    if not FROZEN:
        return
    cleared = 0
    for dirpath, _dirs, files in os.walk(HERE):
        for name in files:
            try:
                os.remove(os.path.join(dirpath, name) + ":Zone.Identifier")
                cleared += 1
            except OSError:
                pass   # no mark (the usual case) or read-only folder
    if cleared:
        log.info("cleared the download mark from %d bundled files", cleared)


def _message_box(text: str):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "AION 2 Vault", 0x40)
    except Exception:
        print(text)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="show a sample inventory instead of capturing")
    ap.add_argument("--browser", action="store_true", help="open in your browser instead of an app window")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="with --browser: don't open a tab")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--diagnose", action="store_true",
                    help="log capture details and save your inventory messages to "
                         "%%APPDATA%%\\AION2Vault\\diagnostics, for troubleshooting missing items")
    args = ap.parse_args()

    os.makedirs(DATA, exist_ok=True)
    handlers = [logging.FileHandler(os.path.join(DATA, "vault.log"), encoding="utf-8")]
    if sys.stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    if not _single_instance():
        _message_box("AION 2 Vault is already running.")
        return
    _unblock_bundle()

    vault = Vault(demo=args.demo, diagnose=args.diagnose)
    vault.start()
    port = _free_port(args.port)
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(vault))
    url = f"http://127.0.0.1:{port}"
    # keep the Windows username out of the log, in case someone shares it for support
    shown = DATA.replace(os.environ.get("APPDATA", "\0"), "%APPDATA%")
    shown = shown.replace(os.path.expanduser("~"), "~")
    log.info("AION 2 Vault %s at %s (data: %s)", VERSION, url, shown)
    import prices as _p
    log.info("HTTPS certificate check: %s", "Windows (truststore)" if type(_p.SSL_CTX).__module__.startswith(
        "truststore") else "Python default")

    if args.browser or args.no_browser:
        print(f"AION 2 Vault running at {url}  (Ctrl+C to stop)")
        if not args.no_browser:
            threading.Timer(0.8, webbrowser.open, (url,)).start()
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
    else:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            _run_window(url)
        except Exception:
            # e.g. WebView2/.NET missing or blocked: keep working in the default browser
            log.exception("app window failed to start; falling back to the browser")
            _run_in_browser(url)
        srv.shutdown()
    if vault.capture:
        vault.capture.stop()


def _tray(on_open, on_quit):
    try:
        from tray import Tray
        return Tray("AION 2 Vault", os.path.join(WEB, "icon.ico"), on_open, on_quit)
    except Exception:
        log.exception("tray icon unavailable")
        return None


def _run_window(url: str):
    import webview
    window = webview.create_window("AION 2 Vault", url, width=1360, height=860, min_size=(900, 560),
                                   background_color="#1b1b1b")
    # closing the window (X) hides it to the tray; Quit in the tray menu exits
    state = {"quitting": False, "told": False}

    def show():
        window.show()
        window.restore()

    def quit_app():
        state["quitting"] = True
        if tray:
            tray.close()
        window.destroy()

    tray = _tray(show, quit_app)

    def on_closing():
        if state["quitting"] or tray is None:
            return True
        window.hide()
        if not state["told"]:
            state["told"] = True
            tray.notify("AION 2 Vault is still running",
                        "It keeps tracking in the tray. Right-click the icon to quit.")
        return False

    window.events.closing += on_closing
    try:
        webview.start(private_mode=False, storage_path=os.path.join(DATA, "webview"))
    finally:
        if tray:
            tray.close()


def _run_in_browser(url: str):
    """Fallback UI: the default browser, with the tray icon to reopen it or quit."""
    done = threading.Event()
    tray = _tray(lambda: webbrowser.open(url), done.set)
    webbrowser.open(url)
    if tray:
        tray.notify("AION 2 Vault opened in your browser",
                    "The app window couldn't start. Click this tray icon to reopen it; right-click to quit.")
    else:
        _message_box("AION 2 Vault opened in your browser because its window couldn't start.\n"
                     "Close this message to quit the app.")
        return
    done.wait()
    tray.close()


if __name__ == "__main__":
    main()
