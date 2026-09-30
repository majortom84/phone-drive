from __future__ import annotations

import errno

import pytest

from phone_drive.connector import Entry, PhoneGone, PhoneLocked
from phone_drive.mtp import MtpConnector
from phone_drive.mtp_lib import ROOT

INTERNAL, SD = 65537, 131073
IS = "/Internal storage"


class FakeDevice:
    def __init__(self, storages=((INTERNAL, "Internal storage"), (SD, "SD card"))):
        self._storages = list(storages)
        self.objects = {}
        self.next_id = 100
        self.closed = False
        self.locked = False

    def add(self, storage, parent, name, is_dir=False, data=b""):
        oid = self.next_id
        self.next_id += 1
        self.objects[oid] = dict(storage=storage, parent=parent, name=name, is_dir=is_dir, data=data)
        return oid

    def named(self, name):
        return [o for o in self.objects.values() if o["name"] == name]

    def friendly_name(self):
        return "Pixel 8"

    def storages(self):
        if self.locked:
            raise PhoneLocked("locked")
        return [(sid, desc, 1000, 400) for sid, desc in self._storages]

    def children(self, storage_id, parent_id):
        return [(oid, o["name"], o["is_dir"], len(o["data"]), 5.0)
                for oid, o in sorted(self.objects.items())
                if o["storage"] == storage_id and o["parent"] == parent_id]

    def get_file(self, oid, local):
        with open(local, "wb") as f:
            f.write(self.objects[oid]["data"])

    def send_file(self, local, name, size, parent_id, storage_id):
        with open(local, "rb") as f:
            data = f.read()
        assert len(data) == size
        return self.add(storage_id, parent_id, name, False, data)

    def create_folder(self, name, parent_id, storage_id):
        return self.add(storage_id, parent_id, name, True)

    def delete(self, oid):
        del self.objects[oid]

    def rename(self, oid, name):
        self.objects[oid]["name"] = name

    def close(self):
        self.closed = True


@pytest.fixture
def dev():
    d = FakeDevice()
    dcim = d.add(INTERNAL, ROOT, "DCIM", True)
    d.add(INTERNAL, dcim, "a.jpg", data=b"photo-a")
    d.add(INTERNAL, ROOT, "Download", True)
    return d


@pytest.fixture
def conn(dev):
    return MtpConnector(dev)


def src_file(tmp_path, data):
    p = tmp_path / "src.bin"
    p.write_bytes(data)
    return str(p)


def test_name_and_storages(conn):
    assert conn.name == "Pixel 8"
    assert conn.storages() == ["Internal storage", "SD card"]
    assert conn.space() == (1000, 400)


def test_locked_phone_has_no_storage():
    with pytest.raises(PhoneLocked):
        MtpConnector(FakeDevice(storages=()))


def test_space_reraises_locked_as_phone_gone(conn, dev):
    dev.locked = True
    with pytest.raises(PhoneGone):
        conn.space()


def test_list_root_and_subfolder(conn):
    assert conn.list_dir(IS) == [Entry("DCIM", True, 0, 5.0), Entry("Download", True, 0, 5.0)]
    assert conn.list_dir(IS + "/DCIM") == [Entry("a.jpg", False, 7, 5.0)]


def test_download_from_cold_cache(conn, tmp_path):
    out = tmp_path / "out.jpg"
    conn.download(IS + "/DCIM/a.jpg", str(out))  # no listing done first
    assert out.read_bytes() == b"photo-a"


def test_stat(conn):
    assert conn.stat(IS) == Entry("Internal storage", True)
    assert conn.stat(IS + "/DCIM/a.jpg").size == 7
    with pytest.raises(FileNotFoundError):
        conn.stat(IS + "/DCIM/nope.jpg")
    with pytest.raises(FileNotFoundError):
        conn.stat("/Nope/x")


def test_upload_goes_to_right_parent_and_storage(conn, dev, tmp_path):
    conn.upload(src_file(tmp_path, b"hi"), IS + "/DCIM/new.txt")
    (obj,) = dev.named("new.txt")
    dcim_id = next(k for k, o in dev.objects.items() if o["name"] == "DCIM")
    assert obj["parent"] == dcim_id and obj["storage"] == INTERNAL and obj["data"] == b"hi"
    conn.upload(src_file(tmp_path, b"sd"), "/SD card/x.txt")
    (sd_obj,) = dev.named("x.txt")
    assert sd_obj["storage"] == SD and sd_obj["parent"] == ROOT


def test_upload_replaces_existing(conn, dev, tmp_path):
    conn.upload(src_file(tmp_path, b"NEW"), IS + "/DCIM/a.jpg")
    (obj,) = dev.named("a.jpg")  # exactly one
    assert obj["data"] == b"NEW"


def test_mkdir(conn, dev):
    conn.mkdir(IS + "/Music")
    (obj,) = dev.named("Music")
    assert obj["is_dir"] and obj["parent"] == ROOT and obj["storage"] == INTERNAL
    with pytest.raises(FileExistsError):
        conn.mkdir(IS + "/Music")


def test_delete(conn, dev):
    with pytest.raises(OSError) as e:
        conn.delete(IS + "/DCIM")
    assert e.value.errno == errno.ENOTEMPTY
    conn.delete(IS + "/DCIM/a.jpg")
    conn.delete(IS + "/DCIM")
    assert dev.named("DCIM") == [] and dev.named("a.jpg") == []


def test_rename_in_same_folder(conn, dev):
    conn.rename(IS + "/DCIM/a.jpg", IS + "/DCIM/b.jpg")
    assert dev.named("a.jpg") == [] and len(dev.named("b.jpg")) == 1
    assert conn.stat(IS + "/DCIM/b.jpg").size == 7


def test_move_file_to_other_folder(conn, dev):
    conn.rename(IS + "/DCIM/a.jpg", IS + "/Download/a.jpg")
    (obj,) = dev.named("a.jpg")
    download_id = next(k for k, o in dev.objects.items() if o["name"] == "Download")
    assert obj["parent"] == download_id and obj["data"] == b"photo-a"


def test_move_folder_to_other_folder_is_refused(conn):
    with pytest.raises(OSError) as e:
        conn.rename(IS + "/DCIM", IS + "/Download/DCIM")
    assert e.value.errno == errno.EXDEV


def test_changes_made_on_phone_show_up(conn, dev):
    conn.list_dir(IS + "/DCIM")
    dcim_id = next(k for k, o in dev.objects.items() if o["name"] == "DCIM")
    dev.add(INTERNAL, dcim_id, "later.jpg", data=b"x")
    assert conn.stat(IS + "/DCIM/later.jpg").size == 1
    dev.delete(next(k for k, o in dev.objects.items() if o["name"] == "a.jpg"))
    with pytest.raises(FileNotFoundError):
        conn.stat(IS + "/DCIM/a.jpg")


def test_close(conn, dev):
    conn.close()
    assert dev.closed


def test_rename_to_same_path_keeps_file(conn, dev):
    conn.rename(IS + "/DCIM/a.jpg", IS + "/DCIM/a.jpg")
    (obj,) = dev.named("a.jpg")
    assert obj["data"] == b"photo-a"


def test_detect_does_not_print_libmtp_chatter(monkeypatch, capfd):
    import os
    from phone_drive import mtp_lib

    class ChattyLib:
        def LIBMTP_Detect_Raw_Devices(self, raw, n):
            os.write(1, b"Device 0 (VID=04e8 and PID=6860) is a Samsung Galaxy models (MTP).\n")
            os.write(2, b"libmtp chatter\n")
            return 5  # LIBMTP_ERROR_NO_DEVICE_ATTACHED

    monkeypatch.setattr(mtp_lib, "_load", lambda: ChattyLib())
    assert mtp_lib.detect() == 0
    out, err = capfd.readouterr()
    assert out == "" and err == ""


def test_libmtp_found_in_either_homebrew(monkeypatch):
    from phone_drive import mtp_lib
    monkeypatch.setattr(mtp_lib.os.path, "exists", lambda p: p == "/opt/homebrew/lib/libmtp.dylib")
    assert mtp_lib.find_libmtp() == "/opt/homebrew/lib/libmtp.dylib"
    monkeypatch.setattr(mtp_lib.os.path, "exists", lambda p: p == "/usr/local/lib/libmtp.dylib")
    assert mtp_lib.find_libmtp() == "/usr/local/lib/libmtp.dylib"
