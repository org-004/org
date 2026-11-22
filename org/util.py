"""Small helpers shared by both sides: atomic JSON files, hashing, ids, human formatting."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Optional


def now() -> float:
    return time.time()


def read_json(path, default=None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError):
        return default


def write_json(path, obj) -> None:
    """Write atomically, so a reader on the far side of a transport never sees half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_text(path, text: str, mode: Optional[int] = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o644 if mode is None else mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_text(path, default: Optional[str] = None) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return default


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            return sha256_bytes(f.read())
    except FileNotFoundError:
        return None


def slugify(text: str, maxlen: int = 28) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(s) > maxlen:
        s = s[:maxlen].rsplit("-", 1)[0] if "-" in s[:maxlen] else s[:maxlen]
    return s.strip("-") or "spec"


def render(template: str, **values) -> str:
    """Fill {{name}} markers. Braces are doubled so prompts can hold JSON and shell untouched."""

    def sub(m):
        key = m.group(1)
        if key not in values:
            raise KeyError("template variable not provided: " + key)
        v = values[key]
        return "" if v is None else str(v)

    return re.sub(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}", sub, template)


# ---------------------------------------------------------------- human formatting


def fmt_dur(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    s = int(max(0, seconds))
    if s < 60:
        return "%ds" % s
    m, s = divmod(s, 60)
    if m < 60:
        return "%dm" % m if m >= 10 else "%dm%02ds" % (m, s)
    h, m = divmod(m, 60)
    if h < 24:
        return "%dh%02dm" % (h, m)
    d, h = divmod(h, 24)
    return "%dd%dh" % (d, h)


def fmt_usd(x: Optional[float]) -> str:
    if x is None:
        return "—"
    if x < 100:
        return "$%.2f" % x
    return "$%.0f" % x


def fmt_pct(frac: Optional[float], digits: int = 0) -> str:
    """Format a 0..1 fraction as a percentage."""
    if frac is None:
        return "—"
    return ("%." + str(digits) + "f%%") % (frac * 100.0)


def fmt_clock(ts: Optional[float], ref: Optional[float] = None) -> str:
    """Wall-clock time in local time: 14:40 today, Tue 14:40 this week, a date otherwise."""
    if ts is None:
        return "—"
    ref = now() if ref is None else ref
    lt, lr = time.localtime(ts), time.localtime(ref)
    if (lt.tm_year, lt.tm_yday) == (lr.tm_year, lr.tm_yday):
        return time.strftime("%H:%M", lt)
    if abs(ts - ref) < 6 * 86400:
        return time.strftime("%a %H:%M", lt)
    return time.strftime("%Y-%m-%d %H:%M", lt)


def fmt_stamp(ts: Optional[float]) -> str:
    if ts is None:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def clip(text: Any, n: int) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"
