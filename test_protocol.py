"""Self-test for the decoder using synthetic frames: python test_protocol.py"""
import struct

import lz4.block

from protocol import (CHANGE_OPCODE, INVENTORY_OPCODE, SOUL_MARK, FrameStream, Inventory,
                      TcpStream)


def varint(v):
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def frame(op, payload):
    body = struct.pack("<H", op) + payload
    return varint(len(body) + 4) + body


def sid(n, hi=0):
    # stack ids: bytes 3, 4 and 7 are zero; byte 6 (hi) grows with the server's counter,
    # and real accounts have 01/02 there (v0.0.3 skipped those, missing most items)
    return bytes([n, 0x22, 0x33, 0, 0, 0x44, hi, 0])


def login_payload(stacks):
    p = b"\x01" * 12
    for s, key, count, cont in stacks:
        p += s + struct.pack("<IQB", key, count, cont) + struct.pack("<H", 3) + b"\x09" * (5 + s[0] % 4)
    return p


def change_payload(recs):
    p = bytes([len(recs)])
    for op, s, key, count, cont in recs:
        rec = bytes([op, 0]) + s + struct.pack("<IQB", key, count, cont) + b"\0" * 18
        p += rec
    return p[:1] + p[1:]


def main():
    keys = {610610015, 514310111, 602460007, 610670043}
    stacks = [(sid(1), 610610015, 25, 1), (sid(2), 514310111, 1, 15), (sid(3), 602460007, 3, 15),
              (sid(4, hi=1), 610670043, 5, 15), (sid(5, hi=2), 610610015, 9, 2)]
    # gear: one soul-bound ring (soul block after the grade) and one unbound bow, both tradable IDs
    ring, bow = 310330019, 110930001
    keys |= {ring, bow}
    gear = (sid(6, 1) + struct.pack("<IQBB", ring, 1, 2, 7) + b"\0" * 30 + b"\x3e" + b"\0" * 4 + b"\x04"
            + b"\0" * 28 + b"\xcc\xee\x05\x00" + SOUL_MARK + b"\0" * 13 + b"\x03" + b"\x11" * 30
            + sid(7, 1) + struct.pack("<IQBB", bow, 1, 2, 2) + b"\0" * 30 + b"\x21" + b"\0" * 4 + b"\x03"
            + b"\0" * 30 + b"\x03" + b"\0" * 20 + b"\x01")
    login = frame(INVENTORY_OPCODE, login_payload(stacks) + gear)
    noise = frame(0x8A05, b"hello world")
    batch = noise + login
    comp = frame(0xFFFF, struct.pack("<I", len(batch)) + lz4.block.compress(batch, store_size=False))
    chg = frame(CHANGE_OPCODE, change_payload([(1, sid(1), 610610015, 30, 1), (2, sid(3), 602460007, 0, 15),
                                               (1, sid(4, hi=1), 610670043, 7, 15),
                                               # a bound item (own ID, not on the market): ignored
                                               (0, sid(8, hi=1), 535400001, 34, 1)]))
    stream = noise + comp + chg

    # deliver as out-of-order, duplicated TCP segments
    t, f, inv = TcpStream(), FrameStream(), Inventory()
    seq0 = 1000
    t.segment(seq0 - 1, b"", syn=True)
    segs = [(seq0 + i, stream[i:i + 7]) for i in range(0, len(stream), 7)]
    order = segs[1::2] + segs[0::2] + segs[:3]
    for seq, data in order:
        for op, payload in f.feed(t.segment(seq, data, False)):
            if op == INVENTORY_OPCODE:
                print("login stacks:", inv.apply_login(payload, keys))
            elif op == CHANGE_OPCODE:
                print("changes:", inv.apply_changes(payload, keys))
    tot = inv.totals()
    print(tot)
    assert not f.broken
    # the soul-bound ring is left out; the unbound bow stays
    assert tot == {(610610015, 1): 30, (514310111, 15): 1, (610670043, 15): 7, (610610015, 2): 9,
                   (bow, 2): 1}, tot
    print("OK")


if __name__ == "__main__":
    main()
