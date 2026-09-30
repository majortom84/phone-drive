"""One log file shared by the watcher and mount processes."""
from __future__ import annotations

import logging
import os

LOG_FILE = os.path.expanduser("~/Library/Logs/PhoneDrive.log")


def setup(role: str) -> None:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    logging.basicConfig(
        filename=LOG_FILE, level=logging.INFO,
        format="%(asctime)s " + role + "[%(process)d] %(levelname)s %(name)s: %(message)s")
