"""Write THIRD-PARTY-NOTICES.txt for the packages bundled into the exe.

usage: python tools/collect_licenses.py <output file>
Permissive licenses (BSD/MIT/PSF) require their notices to ship with binaries.
"""
import importlib.metadata as md
import os
import sys

BUNDLED = ["lz4", "psutil", "pywebview", "pythonnet", "clr_loader", "setuptools", "proxy_tools", "bottle",
           "truststore"]


def license_texts(dist):
    out = []
    for f in dist.files or []:
        name = os.path.basename(str(f)).upper()
        if name.startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE")):
            try:
                with open(f.locate(), encoding="utf-8", errors="replace") as fh:
                    out.append(fh.read())
            except OSError:
                pass
    return out


def main(path):
    parts = ["AION 2 Vault is an unofficial fan tool, not affiliated with or endorsed by NCSOFT.\n"
             "AION and all item names and item icons included in this release are trademarks and\n"
             "copyrighted material of NCSOFT Corporation, used here only to label items.\n"
             "Market prices come from aion2.exchange. Icons for the few market items missing\n"
             "from NCSOFT's item API were taken from aion2.exchange's icon set.\n\n"
             "AION 2 Vault bundles the following third-party software.\n"
             "Npcap is NOT bundled; it is installed separately by the user under its own license.\n"]
    py_license = os.path.join(sys.base_prefix, "LICENSE.txt")
    parts.append(f"\n{'=' * 72}\nPython {sys.version.split()[0]} — Python Software Foundation License\n{'=' * 72}\n")
    if os.path.exists(py_license):
        parts.append(open(py_license, encoding="utf-8", errors="replace").read())
    parts.append(f"\n{'=' * 72}\nPyInstaller bootloader — GPL-2.0 with the PyInstaller bootloader exception,\n"
                 "which allows distributing the built program under any license.\n"
                 "https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt\n")
    for name in BUNDLED:
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            continue
        lic = dist.metadata.get("License-Expression") or dist.metadata.get("License") or ""
        if len(lic) > 80:
            lic = lic.splitlines()[0]
        parts.append(f"\n{'=' * 72}\n{dist.metadata['Name']} {dist.version} — {lic}\n"
                     f"{dist.metadata.get('Home-page') or ''}\n{'=' * 72}\n")
        texts = license_texts(dist)
        parts.append("\n".join(texts) if texts else "(license text not shipped in the package metadata)\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(parts))


if __name__ == "__main__":
    main(sys.argv[1])
