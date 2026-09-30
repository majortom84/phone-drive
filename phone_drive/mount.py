"""Mount one phone: python -m phone_drive.mount --via adb --serial X | --via mtp"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import tempfile
import time

from phone_drive import drive, log as plog
from phone_drive.connector import PhoneGone, PhoneLocked

log = logging.getLogger("phone_drive.mount")

CACHE_ROOT = os.path.expanduser("~/Library/Caches/PhoneDrive")
EXIT_LOCKED = 3
EXIT_GONE = 4


def volume_path(name: str) -> str:
    safe = name.replace("/", "-").replace(":", "-").strip() or "Android phone"
    return "/Volumes/" + safe


def connect(via: str, serial: str):
    if via == "adb":
        from phone_drive.adb import AdbConnector
        return AdbConnector(serial)
    from phone_drive.mtp import MtpConnector
    from phone_drive.mtp_lib import Device
    dev = Device()
    try:
        return MtpConnector(dev)
    except BaseException:
        dev.close()
        raise


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--via", choices=("adb", "mtp"), required=True)
    p.add_argument("--serial")
    a = p.parse_args(argv)
    plog.setup("mount")
    try:
        return _run(a)
    except Exception:
        log.exception("mount failed unexpectedly")
        return 1


def _run(a) -> int:
    try:
        conn = connect(a.via, a.serial)
    except PhoneLocked as e:
        log.warning("%s", e)
        return EXIT_LOCKED
    except (PhoneGone, OSError) as e:
        log.warning("could not connect over %s: %s", a.via, e)
        return EXIT_GONE

    mountpoint = volume_path(conn.name)
    os.makedirs(CACHE_ROOT, exist_ok=True)
    cache = tempfile.mkdtemp(prefix="mount-", dir=CACHE_ROOT)

    def open_in_finder():
        for _ in range(50):
            if os.path.ismount(mountpoint):
                break
            time.sleep(0.1)
        subprocess.run(["open", mountpoint])

    log.info("mounting %s over %s at %s", conn.name, a.via, mountpoint)
    try:
        drive.mount(conn, mountpoint, cache, on_ready=open_in_finder)
    finally:
        conn.close()
        log.info("unmounted %s", mountpoint)
    return 0


if __name__ == "__main__":
    sys.exit(main())
