from __future__ import annotations

import pytest

from phone_drive.mount import EXIT_LOCKED, volume_path
from phone_drive.watcher import Watcher, decide, phonedrive_mounts


def test_decide():
    assert decide(None, "device", True) == "mount-adb"
    assert decide(None, "unauthorized", True) == "mount-mtp"
    assert decide(None, None, True) == "mount-mtp"
    assert decide(None, "unauthorized", False) == "wait"
    assert decide(None, None, False) == "wait"
    assert decide("adb", "device", True) == "wait"
    assert decide("mtp", "device", True) == "wait"  # stay on MTP until unplugged
    assert decide("adb", None, False) == "unmount"
    assert decide("mtp", None, False) == "unmount"


def test_phonedrive_mounts():
    out = ("/dev/disk1s1 on / (apfs, local, journaled)\n"
           "phonedrive on /Volumes/Pixel 8 (macfuse, nodev, nosuid, synchronous, mounted by alice)\n")
    assert phonedrive_mounts(out) == ["/Volumes/Pixel 8"]


def test_volume_path():
    assert volume_path("Pixel 8") == "/Volumes/Pixel 8"
    assert volume_path("a/b:c") == "/Volumes/a-b-c"
    assert volume_path("  ") == "/Volumes/Android phone"


class FakeProc:
    def __init__(self):
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


class Rig:
    def __init__(self):
        self.adb = []
        self.mtp = 0
        self.spawned = []
        self.notes = []
        self.unmounts = 0
        self.clock = 0.0
        self.w = Watcher(adb_devices=lambda: self.adb, mtp_count=lambda: self.mtp,
                         spawn=self.spawn, notify=self.notes.append,
                         unmount=self.unmount, now=lambda: self.clock)

    def spawn(self, via, serial):
        p = FakeProc()
        self.spawned.append((via, serial, p))
        return p

    def unmount(self):
        self.unmounts += 1

    def tick(self, seconds=2.0):
        self.w.tick()
        self.clock += seconds


@pytest.fixture
def rig():
    return Rig()


def test_nothing_plugged_in(rig):
    rig.tick()
    assert rig.spawned == []


def test_mounts_over_adb_when_allowed(rig):
    rig.adb, rig.mtp = [("ABC123XYZ", "device")], 1
    rig.tick()
    rig.tick()
    assert [(v, s) for v, s, _ in rig.spawned] == [("adb", "ABC123XYZ")]


def test_falls_back_to_mtp(rig):
    rig.adb, rig.mtp = [("ABC123XYZ", "unauthorized")], 1
    rig.tick()
    rig.tick()  # grace: MTP only after the phone is seen on two ticks
    assert [(v, s) for v, s, _ in rig.spawned] == [("mtp", "ABC123XYZ")]


def test_unplug_stops_and_unmounts(rig):
    rig.adb, rig.mtp = [("ABC123XYZ", "device")], 1
    rig.tick()
    rig.adb, rig.mtp = [], 0
    rig.tick()
    assert rig.spawned[0][2].terminated
    assert rig.unmounts == 1
    assert rig.w.via is None


def test_locked_phone_notifies_once_and_retries_later(rig):
    rig.mtp = 1
    rig.tick(); rig.tick()                       # t=0 grace, t=2 spawn 1
    rig.spawned[-1][2].returncode = EXIT_LOCKED
    rig.tick()                                   # t=4 sees exit 3, notifies, waits
    assert rig.notes == ["Unlock your phone and tap Allow"]
    assert len(rig.spawned) == 1
    rig.tick(); rig.tick(); rig.tick()           # t=6, 8, 10: retry allowed from t=9 -> spawn 2
    assert len(rig.spawned) == 2
    rig.spawned[-1][2].returncode = EXIT_LOCKED
    rig.tick(); rig.tick(); rig.tick()
    assert rig.notes == ["Unlock your phone and tap Allow"]  # still only once


def test_eject_in_finder_does_not_remount_until_replugged(rig):
    rig.adb = [("ABC123XYZ", "device")]
    rig.tick()
    rig.spawned[-1][2].returncode = 0            # user ejected the drive
    for _ in range(6):
        rig.tick()
    assert len(rig.spawned) == 1
    rig.adb = []                                  # unplug
    rig.tick()
    rig.adb = [("ABC123XYZ", "device")]         # plug back in
    rig.tick()
    assert len(rig.spawned) == 2


def test_mtp_grace_lets_adb_win(rig):
    rig.mtp = 1                                  # MTP is seen instantly, adb not yet
    rig.tick()
    assert rig.spawned == []
    rig.adb = [("ABC123XYZ", "device")]        # adb reports the authorized phone
    rig.tick()
    assert [(v, s) for v, s, _ in rig.spawned] == [("adb", "ABC123XYZ")]
