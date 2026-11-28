"""AION 2 server->client stream decoding: TCP reassembly, framing, inventory messages.

Protocol facts (community-documented, unofficial; may break after patches):
  frame   = varint L (LEB128), then L - 4 bytes of body
  body    = u16le opcode, payload
  0xFFFF  = compressed batch: u32le raw size + LZ4 block -> more frames
  0x5611  = full inventory dump sent at login (bag, warehouse, equipped, ...)
  0x561b  = incremental inventory changes
Server->client traffic is not encrypted; client->server is, and is ignored here.
"""
from __future__ import annotations

import bisect
import collections
import struct
import time

import lz4.block

# Odd things seen while decoding, for the log: e.g. bytes left over inside a compressed batch.
DIAG: collections.Counter = collections.Counter()

COMPRESSED = 0xFFFF
INVENTORY_OPCODE = 0x5611
CHANGE_OPCODE = 0x561B
LENGTH_BIAS = 4
MAX_FRAME = 16 * 1024 * 1024

CONTAINERS = {1: "Inventory", 2: "Server storage", 15: "Character storage",
              11: "Equipped", 4: "Pickup", 18: "Soul Sync recovery"}
COUNTED = {1, 2, 15}   # containers whose items you can actually sell; others aren't totalled
HIDDEN = {4, 18}       # transient pickups and the Soul Sync recovery list: never shown
KINAH_KEY = 930100031


class Desync(Exception):
    pass


# ---------------------------------------------------------------- framing

def _varint(buf: bytes | bytearray, i: int):
    """Return (value, next_index), or None if more bytes are needed."""
    v = 0
    shift = 0
    while True:
        if i >= len(buf):
            return None
        b = buf[i]
        i += 1
        v |= (b & 0x7F) << shift
        if not b & 0x80:
            return v, i
        shift += 7
        if shift > 28:
            raise Desync("varint too long")


def split_frames(buf: bytes | bytearray, depth: int = 0):
    """Yield (opcode, payload) for every complete frame; return bytes consumed.

    Used as a generator; the number of consumed bytes is the StopIteration value.
    """
    j = 0
    n = len(buf)
    while j < n:
        r = _varint(buf, j)
        if r is None:
            break
        length, body = r
        if length < 6 or length - LENGTH_BIAS > MAX_FRAME:
            raise Desync(f"bad frame length {length}")
        end = body + length - LENGTH_BIAS
        if end > n:
            break
        opcode = buf[body] | (buf[body + 1] << 8)
        payload = bytes(buf[body + 2:end])
        j = end
        if opcode == COMPRESSED:
            if depth > 3 or len(payload) < 4:
                raise Desync("bad compressed batch")
            raw = struct.unpack_from("<I", payload, 0)[0]
            if raw > MAX_FRAME:
                raise Desync("compressed batch too large")
            inner = lz4.block.decompress(payload[4:], uncompressed_size=raw)
            DIAG["batches"] += 1
            used = yield from split_frames(inner, depth + 1)
            if used != len(inner):   # a frame inside the batch was cut off: it is dropped
                DIAG["batch_leftover"] += 1
                DIAG["batch_leftover_bytes"] += len(inner) - used
        else:
            yield opcode, payload
    return j


class FrameStream:
    """Accumulates one direction of a TCP stream and emits frames."""

    def __init__(self):
        self.buf = bytearray()
        self.broken = False
        self.error: str | None = None   # why decoding stopped, for the log
        self.frames = 0
        self.bytes = 0

    def feed(self, data: bytes):
        if self.broken:
            return []
        self.buf += data
        self.bytes += len(data)
        out = []
        gen = split_frames(self.buf)
        try:
            while True:
                out.append(next(gen))
        except StopIteration as stop:
            del self.buf[: stop.value or 0]
        except Exception as e:  # Desync, LZ4 errors -> stream unusable until reconnect
            self.broken = True
            self.error = f"{type(e).__name__}: {e} (after {self.frames + len(out)} messages)"
            self.buf.clear()
        self.frames += len(out)
        return out


# ------------------------------------------------------------ reassembly

MOD = 1 << 32


def _seq_diff(a: int, b: int) -> int:
    d = (a - b) % MOD
    return d - MOD if d >= MOD // 2 else d


class TcpStream:
    """Minimal in-order TCP reassembly for one direction."""

    MAX_PENDING = 4 * 1024 * 1024

    def __init__(self):
        self.next_seq: int | None = None
        self.pending: dict[int, bytes] = {}
        self.pending_bytes = 0
        self.gap = False
        self.gap_reason: str | None = None   # for the log
        self.joined_midstream = False
        self.delivered = 0      # bytes handed on in order
        self.duplicates = 0     # segments seen before (retransmits, or the same packet on two adapters)
        self.hole_since: float | None = None   # when we started waiting for a missing segment

    def segment(self, seq: int, data: bytes, syn: bool) -> bytes:
        if syn:
            self.next_seq = (seq + 1) % MOD
            seq = self.next_seq
        if not data:
            return b""
        if self.next_seq is None:
            self.next_seq = seq
            self.joined_midstream = True
        d = _seq_diff(seq, self.next_seq)
        if d < 0:
            if d + len(data) <= 0:
                self.duplicates += 1
                return b""  # retransmission
            data = data[-d:]
            d = 0
        if d > 0:
            if self.hole_since is None:
                self.hole_since = time.time()
            if seq not in self.pending:
                self.pending[seq] = data
                self.pending_bytes += len(data)
            if self.pending_bytes > self.MAX_PENDING:
                self.gap = True
                hole = min(_seq_diff(s, self.next_seq) for s in self.pending)
                self.gap_reason = (f"{hole:,} bytes never arrived after {self.delivered:,} bytes; gave up "
                                   f"after buffering {self.pending_bytes:,} bytes behind the hole")
                self.pending.clear()
                self.pending_bytes = 0
            return b""
        self.hole_since = None
        out = bytearray(data)
        self.next_seq = (self.next_seq + len(data)) % MOD
        while self.pending:
            progressed = False
            for s in list(self.pending):
                dd = _seq_diff(s, self.next_seq)
                if dd <= 0:
                    chunk = self.pending.pop(s)
                    self.pending_bytes -= len(chunk)
                    if dd + len(chunk) > 0:
                        chunk = chunk[-dd:] if dd < 0 else chunk
                        out += chunk
                        self.next_seq = (self.next_seq + len(chunk)) % MOD
                    progressed = True
            if not progressed:
                break
        if self.pending and self.hole_since is None:
            self.hole_since = time.time()   # still waiting for a segment further on
        self.delivered += len(out)
        return bytes(out)


# -------------------------------------------------------------- inventory

KEY_MIN, KEY_MAX = 100_000_000, 1_000_000_000


def parse_login(payload: bytes, known_keys) -> list[tuple[bytes, int, int, int]]:
    """Scan a 0x5611 payload for item stacks -> [(stack_id, key, count, container)].

    Records have variable-length tails, so we scan for a known item key that is
    preceded by a plausible 8-byte stack id (see _stack_id_ok).
    """
    return [(payload[i - 8:i], struct.unpack_from("<I", payload, i)[0],
             struct.unpack_from("<Q", payload, i + 4)[0], payload[i + 12])
            for i in _accepted_offsets(payload, known_keys)]


# Soul-bound gear: once gear is soul-synced it gets soul lines (extra stats and skill levels)
# and can no longer be sold, though it keeps its tradable item ID. Its record then carries a
# soul block that starts with these bytes, shortly after the item level and grade. Unbound
# gear has only zeros there. Seen on bound rings and on Soul Sync recovery gear.
SOUL_MARK = b"\x00\x00\x35\x08\x01\x03"
SOUL_WINDOW = 140              # bytes after the item ID to look in (the mark sits ~85 in)
GEAR_MAX_KEY = 500_000_000     # weapons, armour and accessories have IDs below this


def soul_bound_stacks(payload: bytes, known_keys) -> set[bytes]:
    """Stack ids of gear in a 0x5611 payload that is soul-bound (so it can't be sold)."""
    offs = list(_accepted_offsets(payload, known_keys))
    out = set()
    for i, nxt in zip(offs, offs[1:] + [len(payload) + 8]):
        if struct.unpack_from("<I", payload, i)[0] >= GEAR_MAX_KEY:
            continue
        # look only inside this record: it ends where the next one's stack id starts
        if SOUL_MARK in payload[i + 4:min(i + SOUL_WINDOW, nxt - 8)]:
            out.add(payload[i - 8:i])
    return out


def audit_login(payload: bytes, known_keys) -> dict:
    """For the log: every place a known item ID appears in a 0x5611 payload, and why
    parse_login did or did not take it. A high 'rejected' count means the record layout
    differs from what parse_login assumes; few IDs at all means data went missing."""
    order = list(_accepted_offsets(payload, known_keys))
    accepted = set(order)
    out = {"ids": 0, "accepted": len(accepted), "bad_stack_id": 0, "bad_count": 0,
           "inside_record": 0}
    for i in range(10, len(payload) - 12):
        key = int.from_bytes(payload[i:i + 4], "little")
        if key not in known_keys and key != KINAH_KEY:
            continue
        out["ids"] += 1
        if i in accepted:
            continue
        j = bisect.bisect_left(order, i) - 1
        if j >= 0 and i < order[j] + 13:
            out["inside_record"] += 1          # overlaps a record that was taken
        elif not _stack_id_ok(payload[i - 8:i]):
            out["bad_stack_id"] += 1
        else:
            out["bad_count"] += 1
    return out


def _stack_id_ok(sid: bytes) -> bool:
    """8-byte stack ids look like `xx xx xx 00 00 xx yy 00`: bytes 3, 4 and 7 are zero.
    Byte 6 starts at 0 but grows as the server's counter rises (most stacks on an
    account that has played a while have 01 or 02 there), so it must not be required to be 0."""
    return sid[3] == 0 and sid[4] == 0 and sid[7] == 0


def _accepted_offsets(payload: bytes, known_keys):
    n = len(payload)
    i = 10
    while i <= n - 13:
        if _stack_id_ok(payload[i - 8:i]):
            key = struct.unpack_from("<I", payload, i)[0]
            if key in known_keys or key == KINAH_KEY:
                count = struct.unpack_from("<Q", payload, i + 4)[0]
                limit = 1 << 40 if key == KINAH_KEY else 1_000_000
                if 0 < count < limit:
                    yield i
                    i += 13
                    continue
        i += 1


def _change_at(payload: bytes, p: int):
    if p < 2 or p + 23 > len(payload):
        return None
    if not _stack_id_ok(payload[p:p + 8]):
        return None
    key = struct.unpack_from("<I", payload, p + 8)[0]
    if not KEY_MIN <= key < KEY_MAX:
        return None
    count = struct.unpack_from("<Q", payload, p + 12)[0]
    if count >= 1 << 32:
        return None
    container = payload[p + 20]
    if not 1 <= container <= 63:
        return None
    return payload[p - 2], payload[p:p + 8], key, count, container


def parse_changes(payload: bytes) -> list[tuple[int, bytes, int, int, int]]:
    """Parse a 0x561b payload -> [(op, stack_id, key, count, container)].

    op: 0 = add, 1 = update (count is the new total), 2 = remove.
    """
    if not payload:
        return []
    want = payload[0]
    out = []
    p = 3
    while len(out) < want and p + 23 <= len(payload):
        rec = _change_at(payload, p)
        if rec is None:
            p += 1
            continue
        out.append(rec)
        p += 39
    return out


class Inventory:
    """Item stacks for one login session, keyed by stack id."""

    def __init__(self):
        self.stacks: dict[bytes, dict] = {}
        self.loaded_at: float | None = None
        self.updated_at: float | None = None
        self._last_login = 0.0
        self.last_reset = False   # did the latest 0x5611 clear what was there (for the log)

    def apply_login(self, payload: bytes, known_keys) -> int:
        now = time.time()
        self.last_reset = now - self._last_login > 5
        if self.last_reset:  # new login -> start fresh; bursts merge
            self.stacks = {}
        self._last_login = now
        recs = parse_login(payload, known_keys)
        bound = soul_bound_stacks(payload, known_keys)
        for sid, key, count, cont in recs:
            self.stacks[sid] = {"key": key, "count": count, "container": cont, "bound": sid in bound}
        self.loaded_at = self.updated_at = now
        return len(recs)

    def apply_changes(self, payload: bytes, known_keys=None) -> int:
        """known_keys: market item IDs. Others (bound items such as bound cube keys or Soul
        Codex, which have their own IDs that never appear on the market) are ignored, the
        same as when reading the item list at login."""
        recs = parse_changes(payload)
        for op, sid, key, count, cont in recs:
            if op == 2 or count == 0 or (known_keys is not None and key not in known_keys
                                          and key != KINAH_KEY):
                self.stacks.pop(sid, None)
            else:
                # change records don't carry the soul block: keep what the item list said
                prev = self.stacks.get(sid)
                self.stacks[sid] = {"key": key, "count": count, "container": cont,
                                    "bound": bool(prev and prev.get("bound"))}
        if recs:
            self.updated_at = time.time()
        return len(recs)

    def totals(self) -> dict[tuple[int, int], int]:
        """Aggregate counts per (item key, container). Soul-bound gear is left out: it
        can't be sold, even though its item ID is a tradable one."""
        agg: dict[tuple[int, int], int] = {}
        for s in self.stacks.values():
            if s.get("bound"):
                continue
            k = (s["key"], s["container"])
            agg[k] = agg.get(k, 0) + s["count"]
        return agg
