"""Login helper: watches for the phone, mounts it, unmounts it when unplugged."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from typing import List, Optional

from phone_drive import log as plog
from phone_drive.mount import EXIT_LOCKED

log = logging.getLogger("phone_drive.watcher")

POLL = 2.0
RETRY_DELAY = 5.0
MTP_GRACE_TICKS = 2  # MTP shows up instantly, adb may lag: see the phone this many ticks before choosing MTP
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def decide(mounted_via: Optional[str], adb_state: Optional[str], mtp_present: bool) -> str:
    """'mount-adb', 'mount-mtp', 'unmount' or 'wait'."""
    if mounted_via:
        return "unmount" if adb_state is None and not mtp_present else "wait"
    if adb_state == "device":
        return "mount-adb"
    if mtp_present:
        return "mount-mtp"
    return "wait"


def phonedrive_mounts(mount_output: str) -> List[str]:
    """Mount points of our drives, from `mount` output."""
    found = []
    for line in mount_output.splitlines():
        if line.startswith("phonedrive on ") and " (macfuse" in line:
            found.append(line[len("phonedrive on "):line.rindex(" (macfuse")])
    return found


def unmount_all() -> None:
    text = subprocess.run(["mount"], capture_output=True, text=True).stdout
    for mp in phonedrive_mounts(text):
        log.info("force-unmounting %s", mp)
        subprocess.run(["umount", "-f", mp])


def notify(message: str) -> None:
    subprocess.run(["osascript", "-e",
                    'display notification "%s" with title "PhoneDrive"' % message])


def spawn(via: str, serial: Optional[str]):
    args = [sys.executable, "-m", "phone_drive.mount", "--via", via]
    if via == "adb":
        args += ["--serial", serial]
    log.info("starting mount over %s", via)
    return subprocess.Popen(args, cwd=PROJECT)


def adb_devices():
    from phone_drive.adb import list_devices
    return list_devices()


def mtp_count() -> int:
    from phone_drive.mtp_lib import detect
    return detect()


class Watcher:
    def __init__(self, adb_devices=adb_devices, mtp_count=mtp_count, spawn=spawn,
                 notify=notify, unmount=unmount_all, now=time.monotonic):
        self.adb_devices = adb_devices
        self.mtp_count = mtp_count
        self.spawn = spawn
        self.notify = notify
        self.unmount = unmount
        self.now = now
        self.proc = None
        self.via: Optional[str] = None
        self.retry_at = 0.0
        self.warned = False    # "unlock your phone" already shown for this plug-in
        self.ejected = False   # user ejected in Finder; wait for a re-plug
        self.seen = 0          # consecutive ticks the phone has been visible

    def tick(self) -> None:
        if self.proc is not None and self.proc.poll() is not None:
            code = self.proc.returncode
            log.info("mount process ended with %s", code)
            self.proc, self.via = None, None
            self.unmount()
            if code == 0:
                self.ejected = True
            elif code == EXIT_LOCKED and not self.warned:
                self.notify("Unlock your phone and tap Allow")
                self.warned = True
            self.retry_at = self.now() + RETRY_DELAY

        phones = self.adb_devices()
        serial, state = phones[0] if phones else (None, None)
        mtp = self.mtp_count() > 0
        self.seen = self.seen + 1 if (state is not None or mtp) else 0
        if state is None and not mtp:  # nothing plugged in: reset per-plug-in state
            self.warned = False
            self.ejected = False

        action = decide(self.via, state, mtp)
        if action == "mount-mtp" and self.seen < MTP_GRACE_TICKS:
            action = "wait"  # give adb time to report an authorized phone
        if action == "unmount":
            log.info("phone unplugged")
            self.stop()
        elif action.startswith("mount-") and not self.ejected and self.now() >= self.retry_at:
            self.via = action[len("mount-"):]
            self.proc = self.spawn(self.via, serial)

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc, self.via = None, None
        self.unmount()

    def run(self) -> None:
        self.unmount()  # leftovers from a crash
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("watcher tick failed")
            time.sleep(POLL)


def main() -> None:
    plog.setup("watcher")
    log.info("PhoneDrive watcher started")
    Watcher().run()


if __name__ == "__main__":
    main()
