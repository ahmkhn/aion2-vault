"""Minimal ctypes binding to the user's installed Npcap (wpcap.dll), plus detection.

Npcap is not bundled: users install it themselves from https://npcap.com.
Only the read-only libpcap capture API is used.
"""
from __future__ import annotations

import collections
import ctypes
import ctypes.util
import os
import socket
import struct
import sys
import threading
import time
from ctypes import (POINTER, Structure, c_char, c_char_p, c_int, c_long, c_uint,
                    c_ubyte, c_ushort, c_void_p, byref)

NPCAP_URL = "https://npcap.com/#download"
NPCAP_DIR = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "Npcap")
LOOPBACK = r"\Device\NPF_Loopback"
ERRBUF = 256
SNAPLEN = 65535   # bytes captured per packet; longer (merged) packets are counted in the log

AF_INET, AF_INET6 = 2, 23  # Windows values


class sockaddr(Structure):
    _fields_ = [("sa_family", c_ushort), ("sa_data", c_char * 26)]


class pcap_addr(Structure):
    pass


pcap_addr._fields_ = [("next", POINTER(pcap_addr)), ("addr", POINTER(sockaddr)),
                      ("netmask", POINTER(sockaddr)), ("broadaddr", POINTER(sockaddr)),
                      ("dstaddr", POINTER(sockaddr))]


class pcap_if(Structure):
    pass


pcap_if._fields_ = [("next", POINTER(pcap_if)), ("name", c_char_p), ("description", c_char_p),
                    ("addresses", POINTER(pcap_addr)), ("flags", c_uint)]


class timeval(Structure):
    _fields_ = [("tv_sec", c_long), ("tv_usec", c_long)]


class pcap_pkthdr(Structure):
    _fields_ = [("ts", timeval), ("caplen", c_uint), ("len", c_uint)]


class bpf_program(Structure):
    _fields_ = [("bf_len", c_uint), ("bf_insns", c_void_p)]


class pcap_stat(Structure):
    # ps_recv/ps_drop/ps_ifdrop, plus room for the Windows-only extra field
    _fields_ = [("ps_recv", c_uint), ("ps_drop", c_uint), ("ps_ifdrop", c_uint), ("_win", c_uint)]


_lib = None


def _load():
    global _lib
    if _lib is not None:
        return _lib
    path = os.path.join(NPCAP_DIR, "wpcap.dll")
    if not os.path.exists(path):
        raise FileNotFoundError("Npcap is not installed")
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(NPCAP_DIR)  # Packet.dll lives next to wpcap.dll
    lib = ctypes.CDLL(path)
    lib.pcap_findalldevs.argtypes = [POINTER(POINTER(pcap_if)), c_char_p]
    lib.pcap_freealldevs.argtypes = [POINTER(pcap_if)]
    lib.pcap_open_live.argtypes = [c_char_p, c_int, c_int, c_int, c_char_p]
    lib.pcap_open_live.restype = c_void_p
    lib.pcap_compile.argtypes = [c_void_p, POINTER(bpf_program), c_char_p, c_int, c_uint]
    lib.pcap_setfilter.argtypes = [c_void_p, POINTER(bpf_program)]
    lib.pcap_freecode.argtypes = [POINTER(bpf_program)]
    lib.pcap_next_ex.argtypes = [c_void_p, POINTER(POINTER(pcap_pkthdr)), POINTER(POINTER(c_ubyte))]
    lib.pcap_datalink.argtypes = [c_void_p]
    lib.pcap_geterr.argtypes = [c_void_p]
    lib.pcap_geterr.restype = c_char_p
    lib.pcap_close.argtypes = [c_void_p]
    lib.pcap_breakloop.argtypes = [c_void_p]
    lib.pcap_stats.argtypes = [c_void_p, POINTER(pcap_stat)]
    if hasattr(lib, "pcap_setbuff"):
        lib.pcap_setbuff.argtypes = [c_void_p, c_int]
    _lib = lib
    return lib


# ------------------------------------------------------------ detection

def _admin_only() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Services\npcap\Parameters") as k:
            return bool(winreg.QueryValueEx(k, "AdminOnly")[0])
    except OSError:
        return False


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def status() -> dict:
    """{'state': 'ok'|'missing'|'needs_admin'|'error', 'text': ...}"""
    if not os.path.exists(os.path.join(NPCAP_DIR, "wpcap.dll")):
        return {"state": "missing", "text": "Npcap is not installed"}
    try:
        devs = list_devices()
    except Exception as e:
        return {"state": "error", "text": f"Npcap failed to load: {e}"}
    if not devs:
        if _admin_only() and not is_admin():
            return {"state": "needs_admin", "text": "Npcap is set to administrators only"}
        return {"state": "error", "text": "Npcap found no network adapters"}
    return {"state": "ok", "text": f"Npcap ready ({len(devs)} adapters)"}


def restart_as_admin(args: list[str] | None = None) -> bool:
    """Relaunch this program elevated (UAC prompt). Returns True if it started."""
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, args or sys.argv[1:]
    else:
        exe, params = sys.executable, [os.path.abspath(sys.argv[0])] + (args or sys.argv[1:])
    line = " ".join(f'"{p}"' for p in params)
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, line, None, 1)
    return rc > 32


# -------------------------------------------------------------- devices

def _addr(sa: POINTER(sockaddr)) -> str | None:
    if not sa:
        return None
    fam = sa.contents.sa_family
    raw = ctypes.string_at(ctypes.addressof(sa.contents), 28)
    if fam == AF_INET:
        return socket.inet_ntop(socket.AF_INET, raw[4:8])
    if fam == AF_INET6:
        return socket.inet_ntop(socket.AF_INET6, raw[8:24])
    return None


def list_devices() -> list[dict]:
    lib = _load()
    head = POINTER(pcap_if)()
    err = ctypes.create_string_buffer(ERRBUF)
    if lib.pcap_findalldevs(byref(head), err) != 0:
        raise OSError(err.value.decode(errors="replace"))
    out = []
    try:
        d = head
        while d:
            addrs = []
            a = d.contents.addresses
            while a:
                ip = _addr(a.contents.addr)
                if ip:
                    addrs.append(ip)
                a = a.contents.next
            out.append({"name": d.contents.name.decode(), "desc": (d.contents.description or b"").decode(errors="replace"),
                        "addrs": addrs})
            d = d.contents.next
    finally:
        if head:
            lib.pcap_freealldevs(head)
    return out


# -------------------------------------------------------------- capture

def _parse(linktype: int, pkt: bytes, counts: dict | None = None):
    """Return (src, sport, dst, dport, seq, flags, payload) for TCP packets, else None.

    counts, if given, collects packets whose IP length field doesn't match what was
    captured (their TCP data would come out short or empty)."""
    if linktype == 1:            # Ethernet
        if len(pkt) < 14:
            return None
        etype, off = struct.unpack_from(">H", pkt, 12)[0], 14
        while etype in (0x8100, 0x88A8) and len(pkt) >= off + 4:   # VLAN tags
            etype, off = struct.unpack_from(">H", pkt, off + 2)[0], off + 4
        ver = 4 if etype == 0x0800 else 6 if etype == 0x86DD else 0
    elif linktype == 0:          # loopback (DLT_NULL)
        if len(pkt) < 4:
            return None
        fam, off = struct.unpack_from("<I", pkt, 0)[0], 4
        ver = 4 if fam == 2 else 6 if fam in (23, 24, 28, 30) else 0
    elif linktype in (12, 14, 101):  # raw IP
        off = 0
        ver = pkt[0] >> 4 if pkt else 0
    else:
        return None
    if ver == 4:
        if len(pkt) < off + 20:
            return None
        ihl = (pkt[off] & 0x0F) * 4
        total = struct.unpack_from(">H", pkt, off + 2)[0]
        if pkt[off + 9] != 6:
            return None
        src = socket.inet_ntop(socket.AF_INET, pkt[off + 12:off + 16])
        dst = socket.inet_ntop(socket.AF_INET, pkt[off + 16:off + 20])
        t, end = off + ihl, off + total
    elif ver == 6:
        if len(pkt) < off + 40 or pkt[off + 6] != 6:   # no extension headers expected
            return None
        plen = struct.unpack_from(">H", pkt, off + 4)[0]
        src = socket.inet_ntop(socket.AF_INET6, pkt[off + 8:off + 24])
        dst = socket.inet_ntop(socket.AF_INET6, pkt[off + 24:off + 40])
        t, end = off + 40, off + 40 + plen
    else:
        return None
    if len(pkt) < t + 20:
        return None
    sport, dport, seq = struct.unpack_from(">HHI", pkt, t)
    doff = (pkt[t + 12] >> 4) * 4
    flags = pkt[t + 13]
    if counts is not None:
        if end < t + doff and len(pkt) > t + doff:
            counts["length_field_too_small"] += 1    # e.g. 0 on offloaded/merged packets
        elif end > len(pkt):
            counts["data_cut_off"] += 1              # claims more bytes than were captured
    return src, sport, dst, dport, seq, flags, pkt[t + doff:min(end, len(pkt))]


class Sniffer:
    """Captures TCP on the given adapters in background threads; calls on_packet(tuple)."""

    def __init__(self, on_packet, bpf: str = "tcp"):
        self.on_packet = on_packet
        self.bpf = bpf.encode()
        self.handles: dict[str, int] = {}
        self.errors: dict[str, str] = {}
        self.labels: dict[str, str] = {}    # device -> adapter description, for the log
        # per device: Npcap's own counters plus packets that came through short
        self.counts: dict[str, collections.Counter] = {}
        self._stop = threading.Event()

    def open(self, device: str, label: str = ""):
        if device in self.handles:
            return
        lib = _load()
        err = ctypes.create_string_buffer(ERRBUF)
        self.labels[device] = label or device
        self.counts[device] = collections.Counter()
        h = lib.pcap_open_live(device.encode(), SNAPLEN, 0, 250, err)
        if not h:
            self.errors[device] = err.value.decode(errors="replace")
            raise OSError(self.errors[device])
        if hasattr(lib, "pcap_setbuff"):
            lib.pcap_setbuff(h, 16 * 1024 * 1024)   # fewer drops during the login burst
        prog = bpf_program()
        if lib.pcap_compile(h, byref(prog), self.bpf, 1, 0xFFFFFFFF) == 0:
            lib.pcap_setfilter(h, byref(prog))
            lib.pcap_freecode(byref(prog))
        self.handles[device] = h
        threading.Thread(target=self._loop, args=(device, h), name=f"pcap {device[-12:]}",
                         daemon=True).start()

    def _loop(self, device: str, h):
        lib = _load()
        link = lib.pcap_datalink(h)
        hdr = POINTER(pcap_pkthdr)()
        data = POINTER(c_ubyte)()
        counts = self.counts[device]
        st = pcap_stat()
        stats_at = 0.0
        while not self._stop.is_set():
            r = lib.pcap_next_ex(h, byref(hdr), byref(data))
            now = time.monotonic()
            if now - stats_at > 2:   # read Npcap's counters from this thread (handles aren't shared)
                stats_at = now
                if lib.pcap_stats(h, byref(st)) == 0:
                    counts["npcap_received"] = st.ps_recv
                    counts["npcap_dropped"] = st.ps_drop
                    counts["adapter_dropped"] = st.ps_ifdrop
            if r == 0:
                continue          # timeout
            if r < 0:
                self.errors[device] = (lib.pcap_geterr(h) or b"capture stopped").decode(errors="replace")
                break
            caplen = hdr.contents.caplen
            if caplen < hdr.contents.len:
                counts["longer_than_snaplen"] += 1   # its end was not captured
            pkt = ctypes.string_at(data, caplen)
            try:
                p = _parse(link, pkt, counts)
                if p:
                    self.on_packet(p)
            except Exception:
                counts["handler_errors"] += 1
        lib.pcap_close(h)
        self.handles.pop(device, None)

    def summary(self) -> list[str]:
        """One line per adapter with anything worth reporting, for the log."""
        out = []
        for dev, c in list(self.counts.items()):
            bad = {k: v for k, v in c.items() if v and k != "npcap_received"}
            if c.get("npcap_received") or bad:
                extra = ", ".join(f"{k.replace('_', ' ')} {v:,}" for k, v in sorted(bad.items()))
                out.append(f"{self.labels.get(dev, dev)}: received {c.get('npcap_received', 0):,}"
                           + (f"; {extra}" if extra else "; no drops"))
        return out

    def stop(self):
        self._stop.set()
