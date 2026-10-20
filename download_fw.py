#!/usr/bin/env python3
"""Browse and download DIYLESS OpenTherm Thermostat 3 firmware.

Shows the available versions newest-first, five at a time, and lets you pick one
to download. Uses only the Python standard library.

    python download_fw.py          # interactive picker
    python download_fw.py 3.0.0    # download that exact version, no menu
"""
import json
import sys
import urllib.request
import urllib.error

PRODUCT = "opentherm-thermostat3"
BASE = "https://diyless.com/firmware"
PER_PAGE = 5


def _http(url, method="GET"):
    req = urllib.request.Request(url, data=b"" if method == "POST" else None, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        return e.read()        # the firmware file answers HTTP 404 but with the real body
    except urllib.error.URLError as e:
        sys.exit(f"network error: {e.reason}")


def list_versions():
    """Return available versions, newest first."""
    data = _http(f"{BASE}/versionsv2?productId={PRODUCT}", "POST")
    try:
        vers = [v["ver"] for v in json.loads(data)]
    except Exception:
        sys.exit("couldn't read the version list from the server.")
    return list(reversed(vers))


def download(ver):
    url = f"{BASE}/{PRODUCT}/{ver}/firmware.bin"
    print(f"\nDownloading {ver} ...")
    data = _http(url)
    if not data or data[0] != 0xE9:
        sys.exit(f"that didn't look like a firmware image ({len(data)} bytes). Try again.")
    out = f"firmware-{ver}.bin"
    with open(out, "wb") as f:
        f.write(data)
    print(f"Saved {len(data):,} bytes to {out}")
    print(f"\nNext step:\n  python patch_fw_auto.py {out} patched.bin --floor 35")
    return out


def pick(vers):
    page = 0
    while True:
        start = page * PER_PAGE
        chunk = vers[start:start + PER_PAGE]
        has_older = start + PER_PAGE < len(vers)
        has_newer = page > 0

        print("\n  DIYLESS Thermostat 3 firmware  (newest first)")
        print("  " + "-" * 40)
        for i, v in enumerate(chunk):
            label = "   <- newest" if (page == 0 and i == 0) else ""
            print(f"    {i + 1}. {v}{label}")
        print("  " + "-" * 40)

        nav = []
        if has_older:
            nav.append("[m] older")
        if has_newer:
            nav.append("[n] newer")
        nav.append("[q] quit")
        print("  " + "    ".join(nav))

        choice = input("  Pick a number to download: ").strip().lower()
        if choice == "q":
            return
        if choice == "m" and has_older:
            page += 1
            continue
        if choice == "n" and has_newer:
            page -= 1
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(chunk):
            download(chunk[int(choice) - 1])
            return
        print("  Sorry, didn't catch that.")


def main():
    if len(sys.argv) > 1:
        download(sys.argv[1])
    else:
        pick(list_versions())


if __name__ == "__main__":
    main()
