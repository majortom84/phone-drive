"""Thin ctypes wrapper over Homebrew's libmtp: only what PhoneDrive uses."""
from __future__ import annotations

import contextlib
import ctypes as C
import errno
import logging
import os
from typing import List, Tuple

from phone_drive.connector import PhoneGone, PhoneLocked

log = logging.getLogger("phone_drive.mtp_lib")



def find_libmtp() -> str:
    """Homebrew's libmtp (Apple silicon, then Intel), else let dyld search."""
    for p in ("/opt/homebrew/lib/libmtp.dylib", "/usr/local/lib/libmtp.dylib"):
        if os.path.exists(p):
            return p
    return "libmtp.dylib"


LIBMTP = find_libmtp()
ROOT = 0xFFFFFFFF  # LIBMTP_FILES_AND_FOLDERS_ROOT: the top of a storage
FILETYPE_FOLDER = 0  # LIBMTP_FILETYPE_FOLDER
FILETYPE_UNKNOWN = 44  # LIBMTP_FILETYPE_UNKNOWN


class _File(C.Structure):
    pass


_File._fields_ = [
    ("item_id", C.c_uint32), ("parent_id", C.c_uint32), ("storage_id", C.c_uint32),
    ("filename", C.c_char_p), ("filesize", C.c_uint64), ("modificationdate", C.c_long),
    ("filetype", C.c_int), ("next", C.POINTER(_File)),
]


class _Error(C.Structure):
    pass


_Error._fields_ = [("errornumber", C.c_int), ("error_text", C.c_char_p), ("next", C.POINTER(_Error))]


class _Storage(C.Structure):
    pass


_Storage._fields_ = [
    ("id", C.c_uint32), ("StorageType", C.c_uint16), ("FilesystemType", C.c_uint16),
    ("AccessCapability", C.c_uint16), ("MaxCapacity", C.c_uint64),
    ("FreeSpaceInBytes", C.c_uint64), ("FreeSpaceInObjects", C.c_uint64),
    ("StorageDescription", C.c_char_p), ("VolumeIdentifier", C.c_char_p),
    ("next", C.POINTER(_Storage)), ("prev", C.POINTER(_Storage)),
]


class _Device(C.Structure):
    # Only the leading fields we read; the rest of the struct is never touched.
    _fields_ = [("object_bitsize", C.c_uint8), ("params", C.c_void_p),
                ("usbinfo", C.c_void_p), ("storage", C.POINTER(_Storage))]


class _DeviceEntry(C.Structure):
    _fields_ = [("vendor", C.c_char_p), ("vendor_id", C.c_uint16), ("product", C.c_char_p),
                ("product_id", C.c_uint16), ("device_flags", C.c_uint32)]


class _RawDevice(C.Structure):
    _fields_ = [("device_entry", _DeviceEntry), ("bus_location", C.c_uint32), ("devnum", C.c_uint8)]


_libc = C.CDLL(None)
_libc.free.argtypes = [C.c_void_p]
_lib = None


def _load():
    global _lib
    if _lib is None:
        L = C.CDLL(LIBMTP)
        dev = C.POINTER(_Device)
        sigs = {
            "LIBMTP_Init": ([], None),
            "LIBMTP_Detect_Raw_Devices": ([C.POINTER(C.POINTER(_RawDevice)), C.POINTER(C.c_int)], C.c_int),
            "LIBMTP_Open_Raw_Device_Uncached": ([C.POINTER(_RawDevice)], dev),
            "LIBMTP_Release_Device": ([dev], None),
            "LIBMTP_Get_Storage": ([dev, C.c_int], C.c_int),
            "LIBMTP_Get_Friendlyname": ([dev], C.c_void_p),
            "LIBMTP_Get_Files_And_Folders": ([dev, C.c_uint32, C.c_uint32], C.POINTER(_File)),
            "LIBMTP_destroy_file_t": ([C.POINTER(_File)], None),
            "LIBMTP_Get_File_To_File": ([dev, C.c_uint32, C.c_char_p, C.c_void_p, C.c_void_p], C.c_int),
            "LIBMTP_Send_File_From_File": ([dev, C.c_char_p, C.POINTER(_File), C.c_void_p, C.c_void_p], C.c_int),
            "LIBMTP_Create_Folder": ([dev, C.c_char_p, C.c_uint32, C.c_uint32], C.c_uint32),
            "LIBMTP_Delete_Object": ([dev, C.c_uint32], C.c_int),
            "LIBMTP_Set_Object_Filename": ([dev, C.c_uint32, C.c_char_p], C.c_int),
            "LIBMTP_Get_Errorstack": ([dev], C.POINTER(_Error)),
            "LIBMTP_Clear_Errorstack": ([dev], None),
        }
        for name, (args, res) in sigs.items():
            fn = getattr(L, name)
            fn.argtypes = args
            fn.restype = res
        L.LIBMTP_Init()
        _lib = L
    return _lib


def _raw_devices():
    raw = C.POINTER(_RawDevice)()
    n = C.c_int(0)
    with _quiet():
        err = _load().LIBMTP_Detect_Raw_Devices(C.byref(raw), C.byref(n))
    return raw, (n.value if err == 0 else 0)


@contextlib.contextmanager
def _quiet():
    """Send fd 1 and 2 to /dev/null: libmtp prints a line per detect, every poll."""
    _libc.fflush(None)
    saved = [os.dup(1), os.dup(2)]
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null, 1)
        os.dup2(null, 2)
        yield
    finally:
        _libc.fflush(None)
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        for fd in saved + [null]:
            os.close(fd)


def detect() -> int:
    """How many MTP devices are plugged in. Does not open (or claim) them."""
    raw, n = _raw_devices()
    if raw:
        _libc.free(raw)
    return n


class Device:
    """The first plugged-in MTP device, opened."""

    def __init__(self):
        self.L = _load()
        self._raw, n = _raw_devices()
        if n == 0:
            raise PhoneGone("no MTP device plugged in")
        self._dev = self.L.LIBMTP_Open_Raw_Device_Uncached(self._raw)
        if not self._dev:
            _libc.free(self._raw)
            raise PhoneGone("could not open the MTP device")

    def _log_errorstack(self):
        e = self.L.LIBMTP_Get_Errorstack(self._dev)
        while e:
            log.warning("libmtp error %d: %s", e.contents.errornumber,
                        (e.contents.error_text or b"").decode("utf-8", "replace"))
            e = e.contents.next

    def _failed(self):
        self._log_errorstack()
        self.L.LIBMTP_Clear_Errorstack(self._dev)
        if detect() == 0:
            raise PhoneGone("MTP device gone")
        raise OSError(errno.EIO, "MTP operation failed")

    def friendly_name(self) -> str:
        p = self.L.LIBMTP_Get_Friendlyname(self._dev)
        if not p:
            return ""
        name = C.string_at(p).decode("utf-8", "replace")
        _libc.free(p)
        return name

    def storages(self) -> List[Tuple[int, str, int, int]]:
        if self.L.LIBMTP_Get_Storage(self._dev, 0) != 0:
            self.L.LIBMTP_Clear_Errorstack(self._dev)
            raise PhoneLocked("storage not readable - unlock the phone and tap Allow")
        out = []
        s = self._dev.contents.storage
        while s:
            st = s.contents
            desc = (st.StorageDescription or b"").decode("utf-8", "replace")
            out.append((st.id, desc, st.MaxCapacity, st.FreeSpaceInBytes))
            s = st.next
        return out

    def children(self, storage_id: int, parent_id: int) -> List[Tuple[int, str, bool, int, float]]:
        head = self.L.LIBMTP_Get_Files_And_Folders(self._dev, storage_id, parent_id)
        if not head:
            if self.L.LIBMTP_Get_Errorstack(self._dev):
                self._failed()
            return []  # empty folder
        out = []
        f = head
        while f:
            c = f.contents
            out.append((c.item_id, (c.filename or b"").decode("utf-8", "replace"),
                        c.filetype == FILETYPE_FOLDER, c.filesize, float(c.modificationdate)))
            nxt = c.next  # read before freeing this node
            self.L.LIBMTP_destroy_file_t(f)
            f = nxt
        return out

    def get_file(self, object_id: int, local: str) -> None:
        if self.L.LIBMTP_Get_File_To_File(self._dev, object_id, local.encode(), None, None) != 0:
            self._failed()

    def send_file(self, local: str, name: str, size: int, parent_id: int, storage_id: int) -> int:
        f = _File()
        f.parent_id = parent_id
        f.storage_id = storage_id
        f.filename = name.encode()
        f.filesize = size
        f.filetype = FILETYPE_UNKNOWN
        if self.L.LIBMTP_Send_File_From_File(self._dev, local.encode(), C.byref(f), None, None) != 0:
            self._failed()
        return f.item_id

    def create_folder(self, name: str, parent_id: int, storage_id: int) -> int:
        buf = C.create_string_buffer(name.encode())  # libmtp may edit the name in place
        new_id = self.L.LIBMTP_Create_Folder(self._dev, buf, parent_id, storage_id)
        if new_id == 0:
            self._failed()
        return new_id

    def delete(self, object_id: int) -> None:
        if self.L.LIBMTP_Delete_Object(self._dev, object_id) != 0:
            self._failed()

    def rename(self, object_id: int, new_name: str) -> None:
        buf = C.create_string_buffer(new_name.encode())
        if self.L.LIBMTP_Set_Object_Filename(self._dev, object_id, buf) != 0:
            self._failed()

    def close(self) -> None:
        if self._dev:
            self.L.LIBMTP_Release_Device(self._dev)
            self._dev = None
            _libc.free(self._raw)
