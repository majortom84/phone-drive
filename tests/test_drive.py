from __future__ import annotations

import errno
import os
import stat

import pytest
from mfusepy import FuseOSError

from phone_drive.drive import PhoneFS
from tests.fake_connector import FakeConnector

IS = "/Internal storage"


@pytest.fixture
def phone():
    c = FakeConnector()
    c.dirs.add(IS + "/DCIM")
    c.files[IS + "/DCIM/a.jpg"] = b"photo-a"
    c.files[IS + "/DCIM/b.jpg"] = b"photo-bb"
    return c


@pytest.fixture
def fs(phone, tmp_path):
    return PhoneFS(phone, str(tmp_path / "cache"))


def errno_of(fn, *args):
    with pytest.raises(FuseOSError) as e:
        fn(*args)
    return e.value.errno


def read_all(fs, path):
    fh = fs.open(path, os.O_RDONLY)
    try:
        return fs.read(path, 1 << 20, 0, fh)
    finally:
        fs.release(path, fh)


def write_file(fs, path, data, flags=None):
    fh = fs.create(path, 0o644) if flags is None else fs.open(path, flags)
    fs.write(path, data, 0, fh)
    fs.flush(path, fh)
    fs.release(path, fh)


def test_root_lists_storages(fs):
    assert fs.readdir("/", 0) == [".", "..", "Internal storage", "SD card"]


def test_folder_listing_and_attrs(fs):
    assert fs.readdir(IS + "/DCIM", 0) == [".", "..", "a.jpg", "b.jpg"]
    a = fs.getattr(IS + "/DCIM/b.jpg")
    assert stat.S_ISREG(a["st_mode"]) and a["st_size"] == 8
    assert stat.S_ISDIR(fs.getattr(IS + "/DCIM")["st_mode"])
    assert errno_of(fs.getattr, IS + "/DCIM/nope.jpg") == errno.ENOENT


def test_browsing_downloads_nothing(fs, phone):
    fs.readdir(IS + "/DCIM", 0)
    for name in ("a.jpg", "b.jpg", "nope.jpg"):
        try:
            fs.getattr(IS + "/DCIM/" + name)
        except FuseOSError:
            pass
    fh = fs.open(IS + "/DCIM/a.jpg", os.O_RDONLY)  # opened but never read
    fs.release(IS + "/DCIM/a.jpg", fh)
    assert phone.downloads == []


@pytest.mark.parametrize("proc", ["ImageThumbnailExtension", "AudiovisualThumbnailExtension",
                                  "com.apple.quicklook.ThumbnailsAgent", "mdworker_shared", "mds"])
def test_thumbnailers_and_spotlight_cannot_read(fs, phone, monkeypatch, proc):
    monkeypatch.setattr("phone_drive.drive.caller_name", lambda: proc)
    assert errno_of(fs.open, IS + "/DCIM/a.jpg", os.O_RDONLY) == errno.EACCES
    assert phone.downloads == []


def test_finder_can_still_read(fs, phone, monkeypatch):
    monkeypatch.setattr("phone_drive.drive.caller_name", lambda: "Finder")
    assert read_all(fs, IS + "/DCIM/a.jpg") == b"photo-a"


def test_read_downloads_once(fs, phone):
    assert read_all(fs, IS + "/DCIM/a.jpg") == b"photo-a"
    assert read_all(fs, IS + "/DCIM/a.jpg") == b"photo-a"
    assert phone.downloads == [IS + "/DCIM/a.jpg"]


def test_new_file_uploads_on_flush(fs, phone):
    path = IS + "/DCIM/new.txt"
    fh = fs.create(path, 0o644)
    fs.write(path, b"hello", 0, fh)
    assert fs.getattr(path)["st_size"] == 5  # visible while being written
    assert phone.uploads == []
    fs.flush(path, fh)
    assert phone.files[path] == b"hello"
    fs.release(path, fh)
    assert phone.uploads == [path]  # not uploaded twice
    assert "new.txt" in fs.readdir(IS + "/DCIM", 0)


def test_empty_new_file_is_uploaded(fs, phone):
    path = IS + "/empty.txt"
    fh = fs.create(path, 0o644)
    fs.flush(path, fh)
    fs.release(path, fh)
    assert phone.files[path] == b""


def test_overwrite_existing_file(fs, phone):
    path = IS + "/DCIM/a.jpg"
    read_all(fs, path)  # old copy now cached
    write_file(fs, path, b"NEW", os.O_WRONLY | os.O_TRUNC)
    assert phone.files[path] == b"NEW"
    assert read_all(fs, path) == b"NEW"  # stale cache dropped


def test_truncate_without_handle(fs, phone):
    assert fs.truncate(IS + "/DCIM/b.jpg", 3) == 0
    assert phone.files[IS + "/DCIM/b.jpg"] == b"pho"


def test_folder_ops_refresh_listing(fs):
    fs.readdir(IS, 0)
    fs.mkdir(IS + "/Music", 0o755)
    assert "Music" in fs.readdir(IS, 0)
    fs.rename(IS + "/Music", IS + "/Songs")
    names = fs.readdir(IS, 0)
    assert "Songs" in names and "Music" not in names
    fs.rmdir(IS + "/Songs")
    assert "Songs" not in fs.readdir(IS, 0)
    fs.unlink(IS + "/DCIM/a.jpg")
    assert fs.readdir(IS + "/DCIM", 0) == [".", "..", "b.jpg"]


def test_rmdir_not_empty(fs):
    assert errno_of(fs.rmdir, IS + "/DCIM") == errno.ENOTEMPTY


def test_junk_files_refused(fs, phone):
    assert errno_of(fs.create, IS + "/.DS_Store", 0o644) == errno.EACCES
    assert errno_of(fs.create, IS + "/DCIM/._a.jpg", 0o644) == errno.EACCES
    assert phone.uploads == []


def test_phone_gone_during_read_gives_eio_and_no_cache(fs, phone, tmp_path):
    fh = fs.open(IS + "/DCIM/a.jpg", os.O_RDONLY)
    phone.gone = True
    assert errno_of(fs.read, IS + "/DCIM/a.jpg", 100, 0, fh) == errno.EIO
    assert os.listdir(tmp_path / "cache") == []


def test_phone_gone_during_upload_reaches_finder(fs, phone):
    path = IS + "/x.txt"
    fh = fs.create(path, 0o644)
    fs.write(path, b"data", 0, fh)
    phone.gone = True
    assert errno_of(fs.flush, path, fh) == errno.EIO


def test_statfs_reports_phone_space(fs):
    s = fs.statfs("/")
    assert s["f_bsize"] == 4096
    assert s["f_bavail"] == 60_000_000 // 4096


def test_metadata_changes_are_accepted(fs):
    assert fs.chmod(IS + "/DCIM/a.jpg", 0o600) == 0
    assert fs.chown(IS + "/DCIM/a.jpg", 501, 20) == 0
    assert fs.utimens(IS + "/DCIM/a.jpg", None) == 0


def test_listing_refreshes_after_ttl(fs, phone):
    clock = [0.0]
    fs.now = lambda: clock[0]
    fs.readdir(IS, 0)
    phone.dirs.add(IS + "/Later")  # made on the phone itself
    assert "Later" not in fs.readdir(IS, 0)
    clock[0] = 10.0
    assert "Later" in fs.readdir(IS, 0)


def test_destroy_clears_cache(fs, tmp_path):
    read_all(fs, IS + "/DCIM/a.jpg")
    fs.destroy("/")
    assert not (tmp_path / "cache").exists()


def test_replace_via_truncate_handle_downloads_nothing(fs, phone):
    path = IS + "/DCIM/a.jpg"
    fh = fs.open(path, os.O_WRONLY)  # no O_TRUNC, like Finder
    fs.truncate(path, 0, fh)
    fs.write(path, b"NEW", 0, fh)
    fs.flush(path, fh)
    fs.release(path, fh)
    assert phone.files[path] == b"NEW"
    assert phone.downloads == []


def test_truncate_to_zero_without_handle_downloads_nothing(fs, phone):
    path = IS + "/DCIM/b.jpg"
    assert fs.truncate(path, 0) == 0
    assert phone.files[path] == b""
    assert phone.downloads == []


def test_rdwr_open_downloads_on_first_read(fs, phone):
    path = IS + "/DCIM/a.jpg"
    fh = fs.open(path, os.O_RDWR)
    assert phone.downloads == []
    assert fs.read(path, 100, 0, fh) == b"photo-a"
    fs.release(path, fh)
    assert phone.downloads == [path]


def test_failed_operations_are_logged_but_routine_misses_are_not(caplog):
    import logging
    from phone_drive.drive import log_failed_operations

    fuse_log = logging.getLogger("fuse")
    log_failed_operations()
    msg = "FUSE operation %s (%s) raised a %s, returning errno %s (%s)."
    with caplog.at_level(logging.DEBUG):
        fuse_log.debug(msg, "create", ("/Internal storage/Download/a.txt",), OSError, errno.EACCES, "Permission denied")
        fuse_log.debug(msg, "getattr", ("/Internal storage/nope",), OSError, errno.ENOENT, "No such file")
        fuse_log.debug(msg, "getxattr", ("/x",), OSError, 93, "Attribute not found")
        fuse_log.debug(msg, "open", ("/Internal storage/DCIM/a.jpg",), OSError, errno.EACCES, "Permission denied")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "create" in warnings[0].getMessage() and "a.txt" in warnings[0].getMessage()
    assert [r for r in caplog.records if r.levelno == logging.DEBUG] == []


def test_finder_tags_are_accepted_and_dropped(fs, phone):
    # Refusing them makes macFUSE fall back to ._ files, which noappledouble
    # blocks with EPERM, so Finder fails the whole copy with "no permission".
    path = IS + "/Download/new.txt"
    fh = fs.create(path, 0o644)
    fs.write(path, b"hi", 0, fh)
    fs.release(path, fh)
    assert fs.setxattr(path, "com.apple.metadata:_kMDItemUserTags", b"x", 0) == 0
    assert fs.removexattr(path, "com.apple.metadata:_kMDItemUserTags") == 0
    assert fs.listxattr(path) == []
    assert errno_of(fs.getxattr, path, "com.apple.metadata:_kMDItemUserTags") == 93  # ENOATTR
