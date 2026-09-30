from __future__ import annotations

import errno
import shlex

import pytest

from phone_drive.adb import NO_DIR, AdbConnector, list_devices
from phone_drive.connector import Entry, PhoneGone

SERIAL = "ABC123XYZ"


class FakeRun:
    """Answers adb calls by the first key found in the joined command."""

    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def __call__(self, args, timeout=None):
        self.calls.append(args)
        cmd = " ".join(args)
        for key, reply in self.replies:
            if key in cmd:
                return reply
        return 0, "", ""


BASE = [
    ("device_name", (0, "Pixel 8\n", "")),
    ("ls /storage", (0, "42B4-2C19\nemulated\nself\n", "")),
]


def make(*extra):
    run = FakeRun(list(extra) + BASE)
    return AdbConnector(SERIAL, run), run


def test_list_devices_skips_emulator():
    run = FakeRun([("devices", (0, "List of devices attached\nABC123XYZ\tunauthorized\nemulator-5554\tdevice\n\n", ""))])
    assert list_devices(run) == [("ABC123XYZ", "unauthorized")]


def test_name_and_storages():
    c, run = make()
    assert c.name == "Pixel 8"
    assert c.storages() == ["Internal storage", "SD card"]
    assert c.roots["SD card"] == "/storage/42B4-2C19"
    assert all(call[:2] == ["-s", SERIAL] for call in run.calls)


def test_list_dir_parses_entries():
    out = ("directory|3488|1621565329|/sdcard//DCIM\n"
           "regular file|100|1785981178|/sdcard//a|b.txt\n")
    c, run = make(("find", (0, out, "")))
    assert c.list_dir("/Internal storage") == [
        Entry("DCIM", True, 3488, 1621565329.0),
        Entry("a|b.txt", False, 100, 1785981178.0),
    ]
    assert "find /sdcard/ -mindepth 1 -maxdepth 1" in run.calls[-1][-1]


def test_list_dir_missing_folder():
    c, _ = make(("find", (0, NO_DIR + "\n", "")))
    with pytest.raises(FileNotFoundError):
        c.list_dir("/Internal storage/nope")


def test_sd_card_paths_map_to_storage():
    c, run = make()
    c.list_dir("/SD card/Music")
    assert "/storage/42B4-2C19/Music" in run.calls[-1][-1]


def test_names_are_shell_quoted():
    name = "Bob's clip (1) é.mp4"
    c, run = make(("stat -L", (0, "regular file|5|1|/sdcard/" + name + "\n", "")))
    e = c.stat("/Internal storage/" + name)
    assert e == Entry(name, False, 5, 1.0)
    assert shlex.split(run.calls[-1][-1])[-1] == "/sdcard/" + name


def test_stat_missing_is_file_not_found():
    c, _ = make(("stat -L", (1, "", "stat: '/sdcard/nope': No such file or directory")))
    with pytest.raises(FileNotFoundError):
        c.stat("/Internal storage/nope")


def test_stat_storage_root_uses_storage_name():
    c, _ = make(("stat -L", (0, "directory|3488|5|/sdcard\n", "")))
    assert c.stat("/Internal storage") == Entry("Internal storage", True, 3488, 5.0)


def test_unplugged_is_phone_gone():
    c, _ = make(("find", (1, "", "adb: device 'ABC123XYZ' not found")))
    with pytest.raises(PhoneGone):
        c.list_dir("/Internal storage")


def test_pull_and_push_use_raw_paths():
    c, run = make()
    c.download("/Internal storage/Bob's.mp4", "/tmp/x")
    assert run.calls[-1] == ["-s", SERIAL, "pull", "/sdcard/Bob's.mp4", "/tmp/x"]
    c.upload("/tmp/x", "/SD card/y.txt")
    assert run.calls[-1] == ["-s", SERIAL, "push", "/tmp/x", "/storage/42B4-2C19/y.txt"]


def test_space_from_df():
    df = ("Filesystem 1K-blocks Used Available Use% Mounted on\n"
          "/dev/fuse 112812012 88907916 23773024 79% /storage/emulated\n")
    c, _ = make(("df -k", (0, df, "")))
    assert c.space() == (112812012 * 1024, 23773024 * 1024)


def test_folder_and_file_commands():
    c, run = make()
    c.mkdir("/Internal storage/New Folder")
    assert run.calls[-1][-1] == "mkdir '/sdcard/New Folder'"
    c.delete("/Internal storage/New Folder")
    assert run.calls[-1][-1] == ("if [ -d '/sdcard/New Folder' ]; then rmdir '/sdcard/New Folder'; "
                                 "else rm '/sdcard/New Folder'; fi")
    c.rename("/Internal storage/a b", "/Internal storage/c")
    assert run.calls[-1][-1] == "mv '/sdcard/a b' /sdcard/c"


def test_not_empty_folder_error():
    c, _ = make(("rmdir", (1, "", "rmdir: '/sdcard/A': Directory not empty")))
    with pytest.raises(OSError) as e:
        c.delete("/Internal storage/A")
    assert e.value.errno == errno.ENOTEMPTY


def test_timeout_is_phone_gone():
    c, _ = make(("find", (124, "", "adb timed out")))
    with pytest.raises(PhoneGone):
        c.list_dir("/Internal storage")


def test_error_prefix_is_phone_gone():
    c, _ = make(("find", (1, "", "error: device offline")))
    with pytest.raises(PhoneGone):
        c.list_dir("/Internal storage")


def test_other_failure_is_not_phone_gone():
    # "offline"/"not found" in a shell message must not mean the phone is gone.
    c, _ = make(("mkdir", (1, "", "mkdir: '/sdcard/x': device offline file not found")))
    with pytest.raises(OSError) as e:
        c.mkdir("/Internal storage/x")
    assert not isinstance(e.value, PhoneGone)


def test_adb_found_on_path_then_homebrew(monkeypatch):
    from phone_drive import adb
    monkeypatch.setattr(adb.shutil, "which", lambda name: "/custom/bin/adb")
    monkeypatch.setattr(adb.os.path, "exists", lambda p: True)
    assert adb.find_adb() == "/custom/bin/adb"
    monkeypatch.setattr(adb.shutil, "which", lambda name: None)
    monkeypatch.setattr(adb.os.path, "exists", lambda p: p == "/opt/homebrew/bin/adb")
    assert adb.find_adb() == "/opt/homebrew/bin/adb"
    monkeypatch.setattr(adb.os.path, "exists", lambda p: p == "/usr/local/bin/adb")
    assert adb.find_adb() == "/usr/local/bin/adb"
