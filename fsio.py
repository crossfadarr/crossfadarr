#!/usr/bin/env python3
"""Shared file helpers for the pipeline and app."""
from __future__ import annotations

import json
import os


def write_json_atomic(path: str, obj) -> None:
    """Write JSON via a temp file + os.replace so a reader never sees a partial file.

    The app reads data/*.json on every request while the in-app scan rewrites
    them; os.replace is atomic on Windows and POSIX.
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
