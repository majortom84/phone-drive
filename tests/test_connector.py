from __future__ import annotations

import errno

import pytest

from phone_drive.connector import Entry, PhoneGone, split
from tests.fake_connector import FakeConnector


def test_split_storage_and_rest():
    assert split("/Internal storage/DCIM/a.jpg") == ("Internal storage", "DCIM/a.jpg")
    assert split("/SD card") == ("SD card", "")
    assert split("/SD card/") == ("SD card", "")


def test_fake_round_trip(tmp_path):
    c = FakeConnector()
    c.mkdir("/Internal storage/DCIM")
    src = tmp_path / "a.txt"
    src.write_bytes(b"hello")
    c.upload(str(src), "/Internal storage/DCIM/a.txt")
    assert c.list_dir("/Internal storage") == [Entry("DCIM", True)]
    assert c.stat("/Internal storage/DCIM/a.txt").size == 5
    out = tmp_path / "b.txt"
    c.download("/Internal storage/DCIM/a.txt", str(out))
    assert out.read_bytes() == b"hello"
    assert c.downloads == ["/Internal storage/DCIM/a.txt"]


def test_fake_errors(tmp_path):
    c = FakeConnector()
    c.mkdir("/Internal storage/A")
    c.files["/Internal storage/A/x"] = b"1"
    with pytest.raises(OSError) as e:
        c.delete("/Internal storage/A")
    assert e.value.errno == errno.ENOTEMPTY
    with pytest.raises(FileNotFoundError):
        c.stat("/Internal storage/nope")
    c.gone = True
    with pytest.raises(PhoneGone):
        c.list_dir("/Internal storage")


def test_fake_rename_folder_moves_contents():
    c = FakeConnector()
    c.mkdir("/Internal storage/A")
    c.files["/Internal storage/A/x"] = b"1"
    c.rename("/Internal storage/A", "/Internal storage/B")
    assert c.files == {"/Internal storage/B/x": b"1"}
    assert "/Internal storage/B" in c.dirs and "/Internal storage/A" not in c.dirs
