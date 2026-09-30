"""Connector that talks to the phone over USB debugging (adb)."""
from __future__ import annotations

import dataclasses
import errno
import os
import shlex
import shutil
import subprocess
from typing import Callable, Dict, List, Optional, Tuple

from phone_drive.connector import Entry, PhoneGone, split



def find_adb() -> str:
    """adb from PATH, else Homebrew's (Apple silicon, then Intel)."""
    for p in (shutil.which("adb"), "/opt/homebrew/bin/adb", "/usr/local/bin/adb"):
        if p and os.path.exists(p):
            return p
    return "adb"


ADB = find_adb()
STAT_FMT = "%F|%s|%Y|%n"
NO_DIR = "PHONEDRIVE_NO_SUCH_DIR"

Runner = Callable[[List[str], Optional[float]], Tuple[int, str, str]]


def run_adb(args: List[str], timeout: Optional[float] = None) -> Tuple[int, str, str]:
    try:
        p = subprocess.run([ADB] + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "adb timed out"
    return p.returncode, p.stdout, p.stderr


def list_devices(run: Runner = run_adb) -> List[Tuple[str, str]]:
    """[(serial, state)] of real phones; state is 'device', 'unauthorized', ..."""
    _rc, out, _err = run(["devices"], 15)
    found = []
    for line in out.splitlines()[1:]:
        parts = line.strip().split("\t")
        if len(parts) == 2 and not parts[0].startswith("emulator-"):
            found.append((parts[0], parts[1]))
    return found


def parse_stat(line: str) -> Entry:
    kind, size, mtime, name = line.split("|", 3)
    return Entry(os.path.basename(name.rstrip("/")), kind == "directory", int(size), float(mtime))


class AdbConnector:
    def __init__(self, serial: str, run: Runner = run_adb):
        self.serial = serial
        self._run = run
        name = self._sh("settings get global device_name").strip()
        self.name = name if name and name != "null" else serial
        self.roots: Dict[str, str] = {"Internal storage": "/sdcard"}
        cards = sorted(n for n in self._sh("ls /storage").split() if n not in ("emulated", "self"))
        for i, card in enumerate(cards):
            self.roots["SD card" if i == 0 else "SD card %d" % (i + 1)] = "/storage/" + card

    def _adb(self, args: List[str], timeout: Optional[float] = 60.0) -> str:
        rc, out, err = self._run(["-s", self.serial] + args, timeout)
        if rc == 0:
            return out
        err = err.strip()
        msg = (err + out).strip()
        # Phone-gone only on timeout or adb's own errors (ruling P4).
        if rc == 124 or err.startswith(("adb: ", "error: ")):
            raise PhoneGone(msg)
        for text, code in (("No such file", errno.ENOENT), ("File exists", errno.EEXIST),
                           ("Directory not empty", errno.ENOTEMPTY),
                           ("Permission denied", errno.EACCES)):
            if text in msg:
                raise OSError(code, msg)
        raise OSError(errno.EIO, msg)

    def _sh(self, command: str, timeout: Optional[float] = 60.0) -> str:
        return self._adb(["shell", command], timeout)

    def _remote(self, path: str) -> str:
        storage, rest = split(path)
        if storage not in self.roots:
            raise OSError(errno.ENOENT, path)
        return self.roots[storage] + ("/" + rest if rest else "")

    def storages(self) -> List[str]:
        return list(self.roots)

    def space(self) -> Tuple[int, int]:
        fields = self._sh("df -k /sdcard").strip().splitlines()[-1].split()
        return int(fields[1]) * 1024, int(fields[3]) * 1024

    def list_dir(self, path: str) -> List[Entry]:
        q = shlex.quote(self._remote(path))
        out = self._sh(
            "if [ -d %s ]; then find %s/ -mindepth 1 -maxdepth 1 -exec stat -L -c %s {} + "
            "2>/dev/null || true; else echo %s; fi" % (q, q, shlex.quote(STAT_FMT), NO_DIR))
        if out.strip() == NO_DIR:
            raise OSError(errno.ENOENT, path)
        return [parse_stat(line) for line in out.splitlines() if line.count("|") >= 3]

    def stat(self, path: str) -> Entry:
        q = shlex.quote(self._remote(path))
        e = parse_stat(self._sh("stat -L -c %s %s" % (shlex.quote(STAT_FMT), q)).strip())
        return dataclasses.replace(e, name=os.path.basename(path.rstrip("/")))

    def download(self, path: str, local_file: str) -> None:
        # pull/push paths go to adb's sync service, not a shell: no quoting.
        self._adb(["pull", self._remote(path), local_file], None)

    def upload(self, local_file: str, path: str) -> None:
        self._adb(["push", local_file, self._remote(path)], None)

    def mkdir(self, path: str) -> None:
        self._sh("mkdir " + shlex.quote(self._remote(path)))

    def delete(self, path: str) -> None:
        q = shlex.quote(self._remote(path))
        self._sh("if [ -d %s ]; then rmdir %s; else rm %s; fi" % (q, q, q))

    def rename(self, old: str, new: str) -> None:
        old, new = old.rstrip("/"), new.rstrip("/")
        if old == new:
            return
        # `mv a dir` moves a INTO dir; the MTP connector never does, so refuse.
        if self._sh("[ -d %s ] && echo dir || true" % shlex.quote(self._remote(new))).strip() == "dir":
            raise FileExistsError(errno.EEXIST, "target is an existing folder", new)
        self._sh("mv %s %s" % (shlex.quote(self._remote(old)), shlex.quote(self._remote(new))))

    def close(self) -> None:
        pass
