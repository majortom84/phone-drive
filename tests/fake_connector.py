"""In-memory phone used by the drive tests."""
from __future__ import annotations

import errno
import os
from typing import Dict, List

from phone_drive.connector import Entry, PhoneGone


class FakeConnector:
    name = "Test Phone"

    def __init__(self):
        self.dirs = {"/Internal storage", "/SD card"}
        self.files: Dict[str, bytes] = {}
        self.downloads: List[str] = []
        self.uploads: List[str] = []
        self.gone = False

    def _check(self):
        if self.gone:
            raise PhoneGone("unplugged")

    def storages(self):
        return ["Internal storage", "SD card"]

    def space(self):
        self._check()
        return 100_000_000, 60_000_000

    def list_dir(self, path):
        self._check()
        if path not in self.dirs:
            raise FileNotFoundError(errno.ENOENT, path)
        out = [Entry(os.path.basename(d), True) for d in self.dirs if os.path.dirname(d) == path]
        out += [Entry(os.path.basename(f), False, len(b), 1000.0)
                for f, b in self.files.items() if os.path.dirname(f) == path]
        return sorted(out, key=lambda e: e.name)

    def stat(self, path):
        self._check()
        if path in self.dirs:
            return Entry(os.path.basename(path), True)
        if path in self.files:
            return Entry(os.path.basename(path), False, len(self.files[path]), 1000.0)
        raise FileNotFoundError(errno.ENOENT, path)

    def download(self, path, local_file):
        self._check()
        if path not in self.files:
            raise FileNotFoundError(errno.ENOENT, path)
        self.downloads.append(path)
        with open(local_file, "wb") as f:
            f.write(self.files[path])

    def upload(self, local_file, path):
        self._check()
        with open(local_file, "rb") as f:
            self.files[path] = f.read()
        self.uploads.append(path)

    def mkdir(self, path):
        self._check()
        if path in self.dirs or path in self.files:
            raise FileExistsError(errno.EEXIST, path)
        self.dirs.add(path)

    def delete(self, path):
        self._check()
        if path in self.files:
            del self.files[path]
        elif path in self.dirs:
            if any(os.path.dirname(p) == path for p in list(self.dirs) + list(self.files)):
                raise OSError(errno.ENOTEMPTY, path)
            self.dirs.remove(path)
        else:
            raise FileNotFoundError(errno.ENOENT, path)

    def rename(self, old, new):
        self._check()
        if old in self.files:
            self.files[new] = self.files.pop(old)
        elif old in self.dirs:
            def moved(p):
                return new + p[len(old):] if p == old or p.startswith(old + "/") else p
            self.dirs = {moved(d) for d in self.dirs}
            self.files = {moved(f): b for f, b in self.files.items()}
        else:
            raise FileNotFoundError(errno.ENOENT, old)

    def close(self):
        pass
