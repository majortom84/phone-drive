"""The Finder drive: a FUSE filesystem that shows a phone connector as a disk."""
from __future__ import annotations

import errno
import logging
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import mfusepy as fuse

from phone_drive.connector import Entry, PhoneGone

log = logging.getLogger(__name__)

LISTING_TTL = 5.0  # seconds a folder listing is trusted
BLOCK = 4096


def caller_name() -> str:
    """Name of the process behind the current FUSE request, or "" outside one."""
    try:
        pid = fuse.fuse_get_context()[2]
        out = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)],
                             capture_output=True, text=True).stdout
    except Exception:
        return ""
    return os.path.basename(out.strip())


def is_thumbnailer(name: str) -> bool:
    """Finder thumbnails and Spotlight would read, so download, every file in a folder."""
    return "Thumbnail" in name or name == "mds" or name.startswith("mdworker")


def is_junk(path: str) -> bool:
    """macOS metadata files that must never land on the phone."""
    name = os.path.basename(path)
    return name == ".DS_Store" or name.startswith("._")


ENOATTR = 93  # macOS
ROUTINE_MISSES = {errno.ENOENT, errno.ENOTSUP, ENOATTR}  # Finder probes for these constantly


class _FailedOperations(logging.Filter):
    """mfusepy logs every failed operation at DEBUG; pass the real failures on as warnings."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno > logging.DEBUG:
            return True
        args = record.args
        if not str(record.msg).startswith("FUSE operation") or not isinstance(args, tuple) or len(args) < 4:
            return False
        if args[3] in ROUTINE_MISSES:
            return False
        if args[0] == "open" and args[3] == errno.EACCES:
            return False  # open() logs its own refusals, with the caller
        record.levelno, record.levelname = logging.WARNING, "WARNING"
        record.exc_info = record.exc_text = None
        return True


def log_failed_operations() -> None:
    fuse_log = logging.getLogger("fuse")
    fuse_log.setLevel(logging.DEBUG)
    if not any(isinstance(f, _FailedOperations) for f in fuse_log.filters):
        fuse_log.addFilter(_FailedOperations())


def _remove(local: str) -> None:
    try:
        os.remove(local)
    except FileNotFoundError:
        pass


@dataclass
class Handle:
    path: str
    fd: int  # -1 until the first read of a read-only handle
    local: str
    writable: bool
    dirty: bool = False
    filled: bool = True  # False: writable handle whose temp file is not yet copied from the phone


class PhoneFS(fuse.Operations):
    use_ns = True

    def __init__(self, conn, cache_dir: str, on_ready: Optional[Callable[[], None]] = None):
        self.conn = conn
        self.cache_dir = cache_dir
        self.on_ready = on_ready
        os.makedirs(cache_dir, exist_ok=True)
        self.listings: Dict[str, Tuple[float, List[Entry]]] = {}
        self.cached_files: Dict[str, str] = {}  # phone path -> full local copy
        self.handles: Dict[int, Handle] = {}
        self.next_fh = 1
        self.now = time.monotonic

    # -- helpers

    def _call(self, fn, *args):
        """Run a connector call, turning its errors into FUSE errors."""
        try:
            return fn(*args)
        except PhoneGone as e:
            log.warning("phone gone: %s", e)
            raise fuse.FuseOSError(errno.EIO)
        except FileNotFoundError:
            raise fuse.FuseOSError(errno.ENOENT)
        except FileExistsError:
            raise fuse.FuseOSError(errno.EEXIST)
        except OSError as e:
            raise fuse.FuseOSError(e.errno or errno.EIO)

    def _listing(self, path: str) -> List[Entry]:
        hit = self.listings.get(path)
        if hit and self.now() - hit[0] < LISTING_TTL:
            return hit[1]
        if path == "/":
            entries = [Entry(n, True) for n in self.conn.storages()]
        else:
            entries = self._call(self.conn.list_dir, path)
        self.listings[path] = (self.now(), entries)
        return entries

    def _entry(self, path: str) -> Entry:
        if path == "/":
            return Entry("", True)
        for h in self.handles.values():
            if h.path == path and h.writable:
                return Entry(os.path.basename(path), False, os.fstat(h.fd).st_size, time.time())
        name = os.path.basename(path)
        for e in self._listing(os.path.dirname(path)):
            if e.name == name:
                return e
        raise fuse.FuseOSError(errno.ENOENT)

    def _forget(self, path: str) -> None:
        """Drop cached listings and file copies at or under path, and its parent listing."""
        self.listings.pop(os.path.dirname(path), None)
        for p in [p for p in self.listings if p == path or p.startswith(path + "/")]:
            del self.listings[p]
        for p in [p for p in self.cached_files if p == path or p.startswith(path + "/")]:
            _remove(self.cached_files.pop(p))

    def _fetch(self, path: str) -> str:
        """Full local copy of a phone file, downloaded once per mount."""
        local = self.cached_files.get(path)
        if local:
            return local
        fd, local = tempfile.mkstemp(dir=self.cache_dir)
        os.close(fd)
        log.info("downloading %s for %s", path, caller_name())
        try:
            self._call(self.conn.download, path, local)
        except BaseException:
            _remove(local)
            raise
        self.cached_files[path] = local
        return local

    def _new_handle(self, path, fd, local, writable, dirty=False, filled=True) -> int:
        fh = self.next_fh
        self.next_fh += 1
        self.handles[fh] = Handle(path, fd, local, writable, dirty, filled)
        return fh

    def _fill(self, h: Handle) -> None:
        """Copy the phone's current file into a writable handle's temp file, once."""
        if not h.filled:
            shutil.copyfile(self._fetch(h.path), h.local)  # same inode, so h.fd sees it
            h.filled = True

    def _upload(self, h: Handle) -> None:
        if h.writable and h.dirty:
            self._fill(h)
            os.fsync(h.fd)
            self._call(self.conn.upload, h.local, h.path)
            h.dirty = False
            self._forget(h.path)

    def _writable_handle(self, path: str, fh: Optional[int]) -> Optional[Handle]:
        h = self.handles.get(fh) if fh else None
        if h is not None and h.writable:
            return h
        return next((x for x in self.handles.values() if x.path == path and x.writable), None)

    # -- lifecycle

    def init(self, path):
        if self.on_ready:
            threading.Thread(target=self.on_ready, daemon=True).start()

    def destroy(self, path):
        for h in self.handles.values():
            if h.fd >= 0:
                os.close(h.fd)
        self.handles.clear()
        shutil.rmtree(self.cache_dir, ignore_errors=True)

    # -- reading

    def getattr(self, path, fh=None):
        e = self._entry(path)
        t = int(e.mtime * 1e9)
        common = {"st_mtime": t, "st_ctime": t, "st_atime": t,
                  "st_uid": os.getuid(), "st_gid": os.getgid()}
        if e.is_dir:
            return dict(common, st_mode=stat.S_IFDIR | 0o755, st_nlink=2)
        return dict(common, st_mode=stat.S_IFREG | 0o644, st_nlink=1, st_size=e.size)

    def readdir(self, path, fh):
        return [".", ".."] + [e.name for e in self._listing(path)]

    def open(self, path, flags):
        writable = (flags & (os.O_WRONLY | os.O_RDWR)) != 0
        if not writable:
            self._entry(path)  # ENOENT if missing; download waits for the first read
            caller = caller_name()
            if is_thumbnailer(caller):
                log.info("refused %s to %s", path, caller)
                raise fuse.FuseOSError(errno.EACCES)
            return self._new_handle(path, -1, "", False)
        if is_junk(path):
            raise fuse.FuseOSError(errno.EACCES)
        fd, local = tempfile.mkstemp(dir=self.cache_dir)
        try:
            self._entry(path)  # ENOENT if missing; the download waits until it is needed
        except BaseException:
            os.close(fd)
            _remove(local)
            raise
        trunc = bool(flags & os.O_TRUNC)
        return self._new_handle(path, fd, local, True, dirty=trunc, filled=trunc)

    def read(self, path, size, offset, fh):
        h = self.handles[fh]
        if h.writable:
            self._fill(h)
        if h.fd < 0:
            h.local = self._fetch(h.path)
            h.fd = os.open(h.local, os.O_RDONLY)
        return os.pread(h.fd, size, offset)

    def statfs(self, path):
        total, free = self._call(self.conn.space)
        return {"f_bsize": BLOCK, "f_frsize": BLOCK, "f_blocks": total // BLOCK,
                "f_bfree": free // BLOCK, "f_bavail": free // BLOCK, "f_namemax": 255}

    # -- writing

    def create(self, path, mode, fi=None):
        if is_junk(path):
            raise fuse.FuseOSError(errno.EACCES)
        fd, local = tempfile.mkstemp(dir=self.cache_dir)
        return self._new_handle(path, fd, local, True, dirty=True)

    def write(self, path, data, offset, fh):
        h = self.handles[fh]
        self._fill(h)
        n = os.pwrite(h.fd, data, offset)
        h.dirty = True
        return n

    def truncate(self, path, length, fh=None):
        h = self._writable_handle(path, fh)
        if h is not None:
            if length == 0:
                h.filled = True  # old contents are not needed
            else:
                self._fill(h)
            os.ftruncate(h.fd, length)
            h.dirty = True
            return 0
        if length == 0:
            self._entry(path)  # ENOENT if missing
            fd, local = tempfile.mkstemp(dir=self.cache_dir)
            os.close(fd)
            try:
                self._call(self.conn.upload, local, path)
            finally:
                _remove(local)
            self._forget(path)
            return 0
        tmp = self.open(path, os.O_WRONLY)
        try:
            self._fill(self.handles[tmp])
            os.ftruncate(self.handles[tmp].fd, length)
            self.handles[tmp].dirty = True
            self.flush(path, tmp)
        finally:
            self.release(path, tmp)
        return 0

    def flush(self, path, fh):
        self._upload(self.handles[fh])
        return 0

    def release(self, path, fh):
        h = self.handles.pop(fh)
        try:
            self._upload(h)
        finally:
            if h.fd >= 0:
                os.close(h.fd)
            if h.writable:
                _remove(h.local)
        return 0

    def mkdir(self, path, mode):
        if is_junk(path):
            raise fuse.FuseOSError(errno.EACCES)
        self._call(self.conn.mkdir, path)
        self._forget(path)
        return 0

    def _delete(self, path: str) -> int:
        self._call(self.conn.delete, path)
        self._forget(path)
        return 0

    def rmdir(self, path):
        return self._delete(path)

    def unlink(self, path):
        return self._delete(path)

    def rename(self, old, new):
        if is_junk(new):
            raise fuse.FuseOSError(errno.EACCES)
        self._call(self.conn.rename, old, new)
        self._forget(old)
        self._forget(new)
        return 0

    # Phones have no Unix permissions or settable times; accept and ignore.
    def chmod(self, path, mode):
        return 0

    def chown(self, path, uid, gid):
        return 0

    def utimens(self, path, times=None):
        return 0

    # Nor extended attributes (Finder tags etc.). Refusing them makes macFUSE fall back
    # to ._ files, which noappledouble blocks with EPERM, and Finder fails the copy.
    def setxattr(self, path, name, value, options, position=0):
        return 0

    def removexattr(self, path, name):
        return 0

    def getxattr(self, path, name, position=0):
        raise fuse.FuseOSError(ENOATTR)

    def listxattr(self, path):
        return []


def mount(conn, mountpoint: str, cache_dir: str, on_ready: Optional[Callable[[], None]] = None) -> None:
    """Show the phone as a drive at mountpoint. Blocks until it is unmounted."""
    log_failed_operations()
    fuse.FUSE(PhoneFS(conn, cache_dir, on_ready), mountpoint,
              foreground=True, nothreads=True, fsname="phonedrive", volname=conn.name,
              noappledouble=True,  # no noapplexattr: its EPERM fails Finder copies; setxattr drops them instead
              daemon_timeout=3600)  # whole-file transfers can outlast the 60 s default
