"""Passive capture of the AION 2 client's server->client traffic via Npcap.

Read-only: nothing is sent to the game or its servers, and the game process is
never touched. Only TCP connections owned by the game process are decoded.
"""
from __future__ import annotations

import collections
import ipaddress
import logging
import os
import re
import struct
import threading
import time

import psutil

import npcap
import protocol
from protocol import (CHANGE_OPCODE, CONTAINERS, INVENTORY_OPCODE, KINAH_KEY, FrameStream,
                      Inventory, TcpStream, audit_login, parse_login)

log = logging.getLogger("capture")

GAME_PROCESS = r"^aion2(\.exe)?$"
PENDING_SECONDS = 15            # keep unclaimed streams this long (game conn not seen yet)
PENDING_MAX = 4 * 1024 * 1024   # per stream
DUMP_MAGIC = b"A2VDIAG1"        # diagnostics file: magic, then records of <d time><H opcode><I len> payload
DUMP_MAX = 64 * 1024 * 1024


class GameCapture:
    def __init__(self, inventory: Inventory, lock: threading.RLock, known_keys,
                 on_update, process_pattern: str = GAME_PROCESS,
                 exclude_ports=(443, 80), diag_dir: str | None = None):
        self.inv = inventory
        self.lock = lock
        self.known_keys = known_keys      # callable -> set[int]
        self.on_update = on_update        # callable()
        self.pattern = re.compile(process_pattern, re.I)
        self.exclude_ports = set(exclude_ports)
        self.pids: list[int] = []
        self.conns: set[tuple] = set()    # (local_ip, local_port, remote_ip, remote_port)
        self.streams: dict[tuple, tuple[TcpStream, FrameStream]] = {}
        self.pending: dict[tuple, list] = {}   # key -> [first_seen, size, [(seq, flags, data)], overflowed]
        self.backlog: list[tuple[int, bytes]] = []
        self.sniffer: npcap.Sniffer | None = None
        self.npcap = {"state": "unknown", "text": ""}
        self.error: str | None = None
        self.stats = {"packets": 0, "frames": 0, "logins": 0, "changes": 0}
        self._stop = threading.Event()
        self._devices_at = 0.0
        self._names: dict[tuple, str] = {}      # connection -> short label for the log
        self._keys, self._keys_at = set(), 0.0  # cached known_keys() for the per-message scan
        # diagnostics mode (--diagnose): raw inventory messages go to a file, and every
        # message type is counted, including how many item IDs it seems to contain
        self.diag_dir = diag_dir
        self.dump = None
        self.dump_path: str | None = None
        self.dump_size = 0
        self.opcodes: collections.Counter = collections.Counter()
        self.opcode_items: collections.Counter = collections.Counter()
        self._diag_at = time.time()

    # ------------------------------------------------------------ lifecycle
    def start(self):
        if self.diag_dir:
            os.makedirs(self.diag_dir, exist_ok=True)
            self.dump_path = os.path.join(self.diag_dir, time.strftime("capture-%Y%m%d-%H%M%S.bin"))
            self.dump = open(self.dump_path, "wb")
            self.dump.write(DUMP_MAGIC)
            self.dump.flush()
            log.info("diagnostics on: inventory messages are saved to %s",
                     os.path.join("diagnostics", os.path.basename(self.dump_path)))
        threading.Thread(target=self._watch, name="game-watch", daemon=True).start()

    def stop(self):
        self._stop.set()
        if self.sniffer:
            self.sniffer.stop()

    def recheck(self):
        """Called from the UI after the user installs Npcap."""
        self._devices_at = 0.0

    def _watch(self):
        while not self._stop.is_set():
            try:
                self._ensure_sniffer()
                self._refresh_connections()
                if self.diag_dir and time.time() - self._diag_at > 60:
                    self._diag_at = time.time()
                    self._log_health("every minute")
            except Exception as e:  # keep watching; surface the problem in the UI
                self.error = f"{type(e).__name__}: {e}"
                log.exception("capture watcher")
            self._stop.wait(1 if self.pids else 3)

    def _ensure_sniffer(self):
        now = time.time()
        if now - self._devices_at < 30:
            return
        self._devices_at = now
        self.npcap = npcap.status()
        if self.npcap["state"] != "ok":
            return
        if self.sniffer is None:
            self.sniffer = npcap.Sniffer(self._on_packet, bpf="tcp")
        # every adapter with a routable address, plus loopback (for ExitLag-style proxies)
        for d in npcap.list_devices():
            usable = d["name"] == npcap.LOOPBACK or any(_routable(a) for a in d["addrs"])
            if usable and d["name"] not in self.sniffer.handles:
                label = "Loopback" if d["name"] == npcap.LOOPBACK else (d["desc"] or d["name"])
                try:
                    self.sniffer.open(d["name"], label)
                    log.info("capturing on adapter: %s", label)
                except OSError as e:
                    log.warning("cannot open %s: %s", label, e)
        if not self.sniffer.handles:
            self.npcap = {"state": "error", "text": "Could not open any network adapter: "
                          + "; ".join(self.sniffer.errors.values())}
        self.error = None

    def _refresh_connections(self):
        me = os.getpid()
        pids = []
        for p in psutil.process_iter(["name"]):
            name = p.info.get("name") or ""
            if p.pid != me and self.pattern.search(name):
                pids.append(p.pid)
        conns = set()
        if pids:
            pidset = set(pids)
            mine = [c for c in psutil.net_connections(kind="tcp") if c.pid in pidset]
            local_ports = {c.laddr.port for c in mine if c.laddr}
            for c in mine:
                if c.status != psutil.CONN_ESTABLISHED or not c.raddr:
                    continue
                if c.raddr.port in self.exclude_ports:
                    continue
                if c.raddr.ip.startswith("127.") and c.raddr.port in local_ports:
                    continue  # the game talking to itself
                conns.add((_norm(c.laddr.ip), c.laddr.port, _norm(c.raddr.ip), c.raddr.port))
        now = time.time()
        with self.lock:
            for k in conns - self.conns:
                ent = self.pending.get(k)
                log.info("game connection %s opened (%d packets from before it was spotted%s)",
                         self._name(k), len(ent[2]) if ent else 0,
                         "; some were not kept, the 4 MB early buffer was full" if ent and ent[3] else "")
            for k in self.conns - conns:
                self._log_stream_end(k, "closed")
            self.pids = pids
            self.conns = conns
            for k in [k for k in self.streams if k not in conns]:
                del self.streams[k]
            # replay packets that arrived before we knew the connection was the game's
            for k in [k for k in self.pending if k in conns]:
                for seq, flags, data in self.pending.pop(k)[2]:
                    self._feed(k, seq, flags, data)
            for k in [k for k, v in self.pending.items() if now - v[0] > PENDING_SECONDS]:
                del self.pending[k]

    # -------------------------------------------------------------- packets
    def _on_packet(self, p):
        src, sport, dst, dport, seq, flags, data = p
        # server -> client: packet dst is our local endpoint
        key = (_norm(dst), dport, _norm(src), sport)
        with self.lock:
            if key in self.conns:
                self._feed(key, seq, flags, data)
            elif self.pids and sport not in self.exclude_ports and (data or flags & 0x02):
                ent = self.pending.get(key)
                if ent is None:
                    if len(self.pending) > 256:
                        return
                    ent = self.pending[key] = [time.time(), 0, [], False]
                if ent[1] + len(data) <= PENDING_MAX:
                    ent[1] += len(data)
                    ent[2].append((seq, flags, data))
                else:
                    ent[3] = True

    def _feed(self, key, seq, flags, data):
        self.stats["packets"] += 1
        syn, fin, rst = flags & 0x02, flags & 0x01, flags & 0x04
        st = self.streams.get(key)
        if st is None or syn:
            if st is not None:
                self._log_stream_end(key, "restarted")
            st = (TcpStream(), FrameStream())
            self.streams[key] = st
        tcp_s, frames = st
        chunk = tcp_s.segment(seq, data, bool(syn))
        if tcp_s.gap and not frames.broken:
            frames.broken = True
            frames.error = "lost data: " + (tcp_s.gap_reason or "a packet never arrived")
        if chunk:
            for op, payload in frames.feed(chunk):
                self.stats["frames"] += 1
                if self.diag_dir:
                    self._diag_frame(op, payload)
                if op in (INVENTORY_OPCODE, CHANGE_OPCODE):
                    self._handle(op, payload)
        if frames.broken and not getattr(frames, "reported", False):
            frames.reported = True
            log.warning("game connection %s: stopped reading it, %s. Items sent after this are "
                        "missed until you relog%s", self._name(key), frames.error,
                        " (it was joined mid-session)" if tcp_s.joined_midstream else "")
        if fin or rst:
            self._log_stream_end(key, "closed by the server" if rst else "ended")
            self.streams.pop(key, None)

    def _handle(self, op: int, payload: bytes):
        keys = self.known_keys()
        if not keys:  # item catalog not loaded yet; replay later
            self.backlog.append((op, payload))
            return
        if op == INVENTORY_OPCODE:
            n = self.inv.apply_login(payload, keys)
            self.stats["logins"] += 1
            self._log_login(payload, keys, n, self.inv.last_reset)
        else:
            n = self.inv.apply_changes(payload, keys)
            self.stats["changes"] += 1
            if self.dump:
                self._dump(op, payload)
                log.info("item changes: %d bytes, %d of %d records read", len(payload), n,
                         payload[0] if payload else 0)
        self.on_update()

    def flush_backlog(self):
        with self.lock:
            items, self.backlog = self.backlog, []
            for op, payload in items:
                self._handle(op, payload)

    # ----------------------------------------------------------- diagnostics
    def _name(self, key) -> str:
        if key not in self._names:
            self._names[key] = f"#{len(self._names) + 1} (server port {key[3]})"
        return self._names[key]

    def _log_stream_end(self, key, how):
        st = self.streams.get(key)
        if st is None:
            log.info("game connection %s %s", self._name(key), how)
            return
        t, f = st
        log.info("game connection %s %s: %s bytes in order, %d messages, %d repeated packets%s",
                 self._name(key), how, f"{t.delivered:,}", f.frames, t.duplicates,
                 f"; still waiting on a missing packet ({t.pending_bytes:,} bytes held)" if t.pending else "")

    def _log_login(self, payload: bytes, keys, n: int, reset: bool):
        recs = parse_login(payload, keys)
        per = collections.Counter(CONTAINERS.get(c, f"container {c}") for _, k, _, c in recs
                                  if k != KINAH_KEY)
        kinah = any(k == KINAH_KEY for _, k, _, _ in recs)
        where = ", ".join(f"{name} {cnt}" for name, cnt in per.most_common()) or "none"
        bound = sum(1 for s in self.inv.stacks.values() if s.get("bound"))
        log.info("item list #%d: %s bytes, %d stacks (%s); %d soul-bound gear not counted; Kinah %s; %s",
                 self.stats["logins"], f"{len(payload):,}", n, where, bound, "found" if kinah else "NOT found",
                 "started a new list" if reset else "added to the list from the last few seconds")
        if self.dump:
            self._dump(INVENTORY_OPCODE, payload)
        # the full audit and the adapter counters take a moment; don't hold up capture
        threading.Thread(target=self._audit, args=(payload, keys), daemon=True).start()

    def _audit(self, payload, keys):
        a = audit_login(payload, keys)
        log.info("item list check: %d item IDs present, %d read; skipped %d (stack id doesn't "
                 "look right), %d (count out of range), %d (inside another record)",
                 a["ids"], a["accepted"], a["bad_stack_id"], a["bad_count"], a["inside_record"])
        time.sleep(3)   # let the login burst finish so drops during it are counted
        self._log_health("after item list")

    def _log_health(self, when):
        lines = self.sniffer.summary() if self.sniffer else []
        for line in lines:
            log.info("capture (%s): %s", when, line)
        odd = {k: v for k, v in protocol.DIAG.items() if k.startswith("batch_leftover")}
        if odd:
            log.info("capture (%s): compressed batches with a cut-off message: %s", when, odd)
        if self.diag_dir and self.opcodes:
            with self.lock:
                top = self.opcode_items.most_common(12)
                total = sum(self.opcodes.values())
            if top:
                log.info("message types carrying item IDs (type: messages, IDs): %s",
                         ", ".join(f"{op:#06x}: {self.opcodes[op]}, {c}" for op, c in top))
            log.info("messages decoded so far: %s of %d types", f"{total:,}", len(self.opcodes))

    def _diag_frame(self, op, payload):
        self.opcodes[op] += 1
        if op in (INVENTORY_OPCODE, CHANGE_OPCODE) or len(payload) < 16 or len(payload) > 1 << 20:
            return
        if time.time() - self._keys_at > 10:
            self._keys, self._keys_at = self.known_keys(), time.time()
        keys = self._keys
        hits = 0
        i = payload.find(b"\0\0", 0)
        while 0 <= i <= len(payload) - 6:
            if struct.unpack_from("<I", payload, i + 2)[0] in keys:
                hits += 1
            i = payload.find(b"\0\0", i + 1)
        if hits:
            self.opcode_items[op] += hits

    def _dump(self, op, payload):
        if self.dump_size > DUMP_MAX:
            return
        rec = struct.pack("<dHI", time.time(), op, len(payload)) + payload
        try:
            self.dump.write(rec)
            self.dump.flush()
            self.dump_size += len(rec)
        except OSError as e:
            log.warning("diagnostics file: %s", e)
            self.dump = None

    # --------------------------------------------------------------- status
    def status(self) -> dict:
        with self.lock:
            st = self.npcap.get("state")
            if st == "missing":
                return {"state": "npcap_missing", "text": "Npcap is required for live capture"}
            if st == "needs_admin":
                return {"state": "needs_admin", "text": "Npcap is set to administrators only"}
            if st == "error":
                return {"state": "error", "text": self.npcap["text"]}
            if self.error:
                return {"state": "error", "text": self.error}
            if st != "ok":
                return {"state": "wait", "text": "Starting capture…"}
            if not self.pids:
                return {"state": "idle", "text": "Game not running"}
            if not self.conns:
                return {"state": "idle", "text": "Game running — not connected to a server"}
            if self.inv.loaded_at:
                return {"state": "live", "text": "Live capture"}
            if any(t.joined_midstream for t, _ in self.streams.values()):
                return {"state": "wait", "text": "Connected mid-session — relog or switch "
                        "character to load your storage"}
            return {"state": "wait", "text": "Waiting for character login…"}


def _norm(ip: str) -> str:
    ip = ip.split("%")[0]
    return ip[7:] if ip.startswith("::ffff:") else ip


def _routable(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    return not (a.is_link_local or a.is_loopback or a.is_unspecified)
