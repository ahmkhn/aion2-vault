"""Inspect a diagnostics capture made with `AION2Vault.exe --diagnose`.

    python tools\\analyze_capture.py capture-....bin                   summary + final item list
    python tools\\analyze_capture.py capture-....bin --find Azpha "Fine Asvata Log"
                                                                     where those items appear, raw bytes
    python tools\\analyze_capture.py capture-....bin --rejected       every known item ID the parser skipped
    python tools\\analyze_capture.py capture-....bin --layout         spacing between records the parser took

The capture holds only inventory messages (0x5611 item lists, 0x561b item changes).
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from protocol import (CHANGE_OPCODE, CONTAINERS, INVENTORY_OPCODE, KINAH_KEY,  # noqa: E402
                      Inventory, _accepted_offsets, audit_login, parse_changes)

MAGIC = b"A2VDIAG1"


def read_capture(path):
    with open(path, "rb") as f:
        data = f.read()
    if not data.startswith(MAGIC):
        sys.exit(f"{path}: not an AION 2 Vault diagnostics file")
    i, out = len(MAGIC), []
    while i + 14 <= len(data):
        t, op, n = struct.unpack_from("<dHI", data, i)
        i += 14
        if i + n > len(data):
            print(f"warning: last record cut short ({len(data) - i} of {n} bytes)")
            break
        out.append((t, op, data[i:i + n]))
        i += n
    return out


def load_items():
    names, keys = {}, set()
    db = os.path.join(ROOT, "web", "itemdb.json")
    if os.path.exists(db):
        for k, v in json.load(open(db, encoding="utf-8"))["items"].items():
            keys.add(int(k))
            if v.get("name"):
                names[int(k)] = v["name"]
    for p in (os.path.join(ROOT, "data", "known_keys.json"),
              os.path.expandvars(r"%APPDATA%\AION2Vault\known_keys.json")):
        if os.path.exists(p):
            keys |= set(json.load(open(p)))
    names[KINAH_KEY] = "Kinah"
    return names, keys


def hexdump(b: bytes) -> str:
    return " ".join(f"{x:02x}" for x in b)


def show_at(payload, i, accepted):
    lo, hi = max(0, i - 16), min(len(payload), i + 24)
    count = struct.unpack_from("<Q", payload, i + 4)[0] if i + 12 <= len(payload) else None
    cont = payload[i + 12] if i + 12 < len(payload) else None
    tag = "READ   " if i in accepted else "SKIPPED"
    print(f"    {tag} @{i:<7} count(u64 at +4)={count}  u32 at +4={struct.unpack_from('<I', payload, i + 4)[0] if i + 8 <= len(payload) else None}"
          f"  container(+12)={cont}")
    print(f"      before: {hexdump(payload[lo:i])}")
    print(f"      ID    : {hexdump(payload[i:i + 4])}")
    print(f"      after : {hexdump(payload[i + 4:hi])}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("capture")
    ap.add_argument("--find", nargs="+", metavar="NAME_OR_ID", help="show raw bytes around these items")
    ap.add_argument("--rejected", action="store_true", help="list skipped occurrences of known item IDs")
    ap.add_argument("--layout", action="store_true", help="spacing between records the parser read")
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args()

    names, keys = load_items()
    recs = read_capture(args.capture)
    t0 = recs[0][0] if recs else 0
    lists = [(t, p) for t, op, p in recs if op == INVENTORY_OPCODE]
    changes = [(t, p) for t, op, p in recs if op == CHANGE_OPCODE]
    print(f"{len(lists)} item lists (0x5611), {len(changes)} change messages (0x561b), "
          f"{len(keys)} known item IDs\n")

    inv = Inventory()
    last_list = None
    for t, op, p in recs:
        if op == INVENTORY_OPCODE:
            # the app starts a new list when one arrives >5 s after the last; replay that
            # using the capture's timestamps rather than the clock now
            if last_list is not None and t - last_list > 5:
                inv._last_login = 0.0
            last_list = t
            n = inv.apply_login(p, keys)
            per = collections.Counter(CONTAINERS.get(c, f"container {c}")
                                      for c in (p[i + 12] for i in _accepted_offsets(p, keys)))
            a = audit_login(p, keys)
            print(f"+{t - t0:7.1f}s item list: {len(p):,} bytes, {n} stacks "
                  f"({', '.join(f'{k} {v}' for k, v in per.most_common())}); "
                  f"{'new list' if inv.last_reset else 'merged'}; IDs present {a['ids']}, "
                  f"skipped: stack id {a['bad_stack_id']}, count {a['bad_count']}, "
                  f"inside {a['inside_record']}")
        else:
            got = parse_changes(p)
            inv.apply_changes(p, keys)
            print(f"+{t - t0:7.1f}s changes: {len(got)}/{p[0] if p else 0} read "
                  + ", ".join(f"{['add', 'upd', 'del'][o] if o < 3 else o}:{names.get(k, k)}x{c}@{ct}"
                              for o, _, k, c, ct in got))

    print("\nFinal item list (what the app would show):")
    tot = collections.defaultdict(collections.Counter)
    for s in inv.stacks.values():
        if s.get("bound"):
            print(f"  (soul-bound, not counted: {names.get(s['key'], s['key'])} in "
                  f"{CONTAINERS.get(s['container'], s['container'])})")
            continue
        tot[CONTAINERS.get(s["container"], f"container {s['container']}")][s["key"]] += s["count"]
    for cont, items in sorted(tot.items()):
        print(f"  {cont}:")
        for k, c in sorted(items.items(), key=lambda kv: names.get(kv[0], str(kv[0]))):
            print(f"    {names.get(k, '?'):40} {k}  x{c}")

    if args.find:
        want = set()
        for q in args.find:
            if q.isdigit():
                want.add(int(q))
                continue
            exact = {k for k, n in names.items() if n.lower() == q.lower()}
            want |= exact or {k for k, n in names.items() if q.lower() in n.lower()}
        for k in sorted(want):
            needle = struct.pack("<I", k)
            print(f"\n== {names.get(k, '?')} ({k}){'' if k in keys else '  [not a known ID]'}")
            seen = False
            for li, (t, p) in enumerate(lists):
                acc = set(_accepted_offsets(p, keys))
                i = p.find(needle)
                while i >= 0:
                    seen = True
                    print(f"  item list #{li + 1}:")
                    show_at(p, i, acc)
                    i = p.find(needle, i + 1)
            for t, p in changes:
                if needle in p:
                    seen = True
                    print(f"  in a change message at +{t - t0:.1f}s at offset {p.find(needle)}")
            if not seen:
                print("  not in any captured message: the data never arrived, or comes in another message type")

    if args.rejected:
        shown = 0
        for li, (t, p) in enumerate(lists):
            acc = set(_accepted_offsets(p, keys))
            for i in range(10, len(p) - 12):
                k = int.from_bytes(p[i:i + 4], "little")
                if (k in keys or k == KINAH_KEY) and i not in acc:
                    if shown >= args.limit:
                        break
                    shown += 1
                    print(f"\nitem list #{li + 1}: {names.get(k, '?')} ({k})")
                    show_at(p, i, acc)

    if args.layout:
        for li, (t, p) in enumerate(lists):
            offs = list(_accepted_offsets(p, keys))
            gaps = collections.Counter(b - a for a, b in zip(offs, offs[1:]))
            print(f"\nitem list #{li + 1}: first record at {offs[0] if offs else '-'}, "
                  f"spacing (bytes: times) {dict(gaps.most_common(15))}")


if __name__ == "__main__":
    main()
