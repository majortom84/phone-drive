from __future__ import annotations

import argparse
import ctypes as C
import logging

import pytest

from phone_drive import drive, mount
from phone_drive import mtp_lib
from phone_drive.adb import AdbConnector
from phone_drive.connector import PhoneGone
from tests.fake_connector import FakeConnector
from tests.test_adb import SERIAL, FakeRun, BASE, make


def test_mount_sets_daemon_timeout(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(drive.fuse, "FUSE", lambda *a, **k: seen.update(k))
    drive.mount(FakeConnector(), str(tmp_path / "mnt"), str(tmp_path))
    assert seen["daemon_timeout"] == 3600


def test_main_logs_unexpected_exception(monkeypatch, caplog):
    monkeypatch.setattr(mount.plog, "setup", lambda name: None)

    def boom(via, serial):
        raise ValueError("kaboom")

    monkeypatch.setattr(mount, "connect", boom)
    with caplog.at_level(logging.ERROR, logger="phone_drive.mount"):
        assert mount.main(["--via", "adb", "--serial", "X"]) == 1
    assert any("kaboom" in (r.exc_text or "") or r.exc_info for r in caplog.records)


def test_failed_logs_errorstack(monkeypatch, caplog):
    err = mtp_lib._Error(errornumber=7, error_text=b"PTP Layer error 02fe", next=None)

    class L:
        cleared = False

        def LIBMTP_Get_Errorstack(self, dev):
            return C.pointer(err)

        def LIBMTP_Clear_Errorstack(self, dev):
            L.cleared = True

    d = mtp_lib.Device.__new__(mtp_lib.Device)
    d.L, d._dev = L(), object()
    monkeypatch.setattr(mtp_lib, "detect", lambda: 1)
    with caplog.at_level(logging.WARNING, logger="phone_drive.mtp_lib"):
        with pytest.raises(OSError):
            d._failed()
    assert "PTP Layer error 02fe" in caplog.text
    assert L.cleared


def test_stderr_leading_whitespace_still_phone_gone():
    c, _ = make(("find", (1, "", "\n  error: device offline\n")))
    with pytest.raises(PhoneGone):
        c.list_dir("/Internal storage")


def test_rename_onto_existing_folder_refused():
    c, run = make(("[ -d /sdcard/Pics ]", (0, "dir\n", "")))
    with pytest.raises(FileExistsError):
        c.rename("/Internal storage/a.txt", "/Internal storage/Pics")
    assert not any(x[-1].startswith("mv ") for x in run.calls)
