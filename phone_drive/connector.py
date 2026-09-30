"""What every phone connector (adb, MTP) provides to the Finder drive."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

try:
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object


@dataclass(frozen=True)
class Entry:
    name: str
    is_dir: bool
    size: int = 0
    mtime: float = 0.0


class PhoneGone(Exception):
    """The phone was unplugged or stopped answering."""


class PhoneLocked(Exception):
    """The phone is there but its storage can't be read yet (locked / not allowed)."""


class Connector(Protocol):
    """Paths are always '/<storage name>/<rest>'.

    Raises FileNotFoundError, FileExistsError, OSError(errno...) or PhoneGone.
    """

    name: str

    def storages(self) -> List[str]: ...
    def space(self) -> Tuple[int, int]: ...  # (total, free) bytes of the first storage
    def list_dir(self, path: str) -> List[Entry]: ...
    def stat(self, path: str) -> Entry: ...
    def download(self, path: str, local_file: str) -> None: ...
    def upload(self, local_file: str, path: str) -> None: ...  # replaces an existing file
    def mkdir(self, path: str) -> None: ...
    def delete(self, path: str) -> None: ...  # a file or an empty folder
    def rename(self, old: str, new: str) -> None: ...
    def close(self) -> None: ...


def split(path: str) -> Tuple[str, str]:
    """'/Internal storage/DCIM/a.jpg' -> ('Internal storage', 'DCIM/a.jpg')."""
    parts = path.strip("/").split("/", 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")
