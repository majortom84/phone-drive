"""Connector that talks to the phone in normal 'File transfer' mode (MTP)."""
from __future__ import annotations

import errno
import os
import tempfile
from typing import Dict, List, Tuple

from phone_drive.connector import Entry, PhoneGone, PhoneLocked, split
from phone_drive.mtp_lib import ROOT


class MtpConnector:
    def __init__(self, device):
        self.dev = device
        self.name = device.friendly_name() or "Android phone"
        self.stores: Dict[str, int] = {}
        for sid, desc, _total, _free in device.storages():
            self.stores[desc or "Storage %x" % sid] = sid
        if not self.stores:
            raise PhoneLocked("no readable storage - unlock the phone and tap Allow")
        self.ids: Dict[str, Tuple[int, bool]] = {}  # phone path -> (object id, is folder)

    # -- path <-> object id

    def _storage_id(self, path: str) -> int:
        storage, _rest = split(path)
        if storage not in self.stores:
            raise FileNotFoundError(errno.ENOENT, path)
        return self.stores[storage]

    def _folder(self, path: str) -> Tuple[int, int]:
        """(storage id, object id) of a folder; a storage's top is ROOT."""
        sid = self._storage_id(path)
        if not split(path)[1]:
            return sid, ROOT
        oid, is_dir = self._lookup(path)
        if not is_dir:
            raise OSError(errno.ENOTDIR, path)
        return sid, oid

    def _lookup(self, path: str) -> Tuple[int, bool]:
        path = path.rstrip("/")
        if path not in self.ids:
            self.list_dir(os.path.dirname(path))  # refresh the parent's children
        if path not in self.ids:
            raise FileNotFoundError(errno.ENOENT, path)
        return self.ids[path]

    def _drop(self, path: str) -> None:
        for p in [p for p in self.ids if p == path or p.startswith(path + "/")]:
            del self.ids[p]

    def _remove_if_exists(self, path: str) -> None:
        try:
            oid, is_dir = self._lookup(path)
        except FileNotFoundError:
            return
        if is_dir:
            raise OSError(errno.EISDIR, path)
        self.dev.delete(oid)
        self._drop(path)

    # -- Connector interface

    def storages(self) -> List[str]:
        return list(self.stores)

    def space(self) -> Tuple[int, int]:
        try:
            _sid, _desc, total, free = self.dev.storages()[0]
        except PhoneLocked as e:
            # The drive does not map PhoneLocked, so report it as a lost phone.
            raise PhoneGone(str(e)) from e
        return total, free

    def list_dir(self, path: str) -> List[Entry]:
        path = path.rstrip("/")
        sid, parent = self._folder(path)
        for p in [p for p in self.ids if os.path.dirname(p) == path]:
            del self.ids[p]
        entries = []
        for oid, name, is_dir, size, mtime in self.dev.children(sid, parent):
            self.ids[path + "/" + name] = (oid, is_dir)
            entries.append(Entry(name, is_dir, size, mtime))
        return entries

    def stat(self, path: str) -> Entry:
        path = path.rstrip("/")
        storage, rest = split(path)
        self._storage_id(path)
        if not rest:
            return Entry(storage, True)
        name = os.path.basename(path)
        for e in self.list_dir(os.path.dirname(path)):
            if e.name == name:
                return e
        raise FileNotFoundError(errno.ENOENT, path)

    def download(self, path: str, local_file: str) -> None:
        oid, is_dir = self._lookup(path)
        if is_dir:
            raise OSError(errno.EISDIR, path)
        self.dev.get_file(oid, local_file)

    def upload(self, local_file: str, path: str) -> None:
        path = path.rstrip("/")
        sid, parent = self._folder(os.path.dirname(path))
        self._remove_if_exists(path)  # MTP cannot overwrite in place
        oid = self.dev.send_file(local_file, os.path.basename(path),
                                 os.path.getsize(local_file), parent, sid)
        self.ids[path] = (oid, False)

    def mkdir(self, path: str) -> None:
        path = path.rstrip("/")
        sid, parent = self._folder(os.path.dirname(path))
        try:
            self._lookup(path)
        except FileNotFoundError:
            self.ids[path] = (self.dev.create_folder(os.path.basename(path), parent, sid), True)
            return
        raise FileExistsError(errno.EEXIST, path)

    def delete(self, path: str) -> None:
        path = path.rstrip("/")
        oid, is_dir = self._lookup(path)
        if is_dir and self.list_dir(path):
            raise OSError(errno.ENOTEMPTY, path)
        self.dev.delete(oid)
        self._drop(path)

    def rename(self, old: str, new: str) -> None:
        old, new = old.rstrip("/"), new.rstrip("/")
        if old == new:
            return
        oid, is_dir = self._lookup(old)
        if os.path.dirname(old) != os.path.dirname(new):
            if is_dir:
                raise OSError(errno.EXDEV, "MTP cannot move folders; copy instead")
            with tempfile.TemporaryDirectory() as tmp:  # move a file = copy + delete
                local = os.path.join(tmp, "moving")
                self.dev.get_file(oid, local)
                self.upload(local, new)
            self.dev.delete(oid)
            self._drop(old)
            return
        if not is_dir:
            self._remove_if_exists(new)
        self.dev.rename(oid, os.path.basename(new))
        self._drop(old)
        self.ids[new] = (oid, is_dir)

    def close(self) -> None:
        self.dev.close()
