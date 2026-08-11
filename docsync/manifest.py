"""Manifest of everything already fetched, so runs can be incremental."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from .util import LOG

MANIFEST_NAME = ".docsync-manifest.json"
SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Entry:
    key: str
    source: str = ""
    product: str = ""
    doc_version: str = ""
    title: str = ""
    url: str = ""
    path: str = ""
    method: str = ""  # pdf | converted
    size: Optional[int] = None
    sha256: Optional[str] = None
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    fingerprint: Optional[str] = None
    fetched_at: str = field(default_factory=_now)

    @classmethod
    def from_dict(cls, data: Dict) -> "Entry":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


class Manifest:
    """JSON index kept next to the downloaded files."""

    def __init__(self, base_dir: Path, filename: str = MANIFEST_NAME) -> None:
        self.base_dir = Path(base_dir)
        self.path = self.base_dir / filename
        self.entries: Dict[str, Entry] = {}
        self.created_at = _now()
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("could not read manifest %s (%s); starting a fresh one", self.path, exc)
            return
        self.created_at = data.get("created_at", self.created_at)
        for key, raw in (data.get("entries") or {}).items():
            try:
                self.entries[key] = Entry.from_dict({**raw, "key": key})
            except TypeError:
                LOG.debug("skipping malformed manifest entry %s", key)

    def save(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": SCHEMA_VERSION,
            "created_at": self.created_at,
            "updated_at": _now(),
            "count": len(self.entries),
            "entries": {k: {kk: vv for kk, vv in asdict(v).items() if kk != "key"}
                        for k, v in sorted(self.entries.items())},
        }
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(self.base_dir), prefix=".manifest-", suffix=".tmp", delete=False
        )
        try:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            os.replace(handle.name, self.path)
        except Exception:
            handle.close()
            Path(handle.name).unlink(missing_ok=True)
            raise

    # -- queries ----------------------------------------------------------
    def get(self, key: str) -> Optional[Entry]:
        return self.entries.get(key)

    def put(self, entry: Entry) -> None:
        entry.fetched_at = _now()
        self.entries[entry.key] = entry

    def file_exists(self, key: str) -> bool:
        entry = self.entries.get(key)
        if not entry or not entry.path:
            return False
        candidate = Path(entry.path)
        if not candidate.is_absolute():
            candidate = self.base_dir / candidate
        return candidate.exists()

    def known(self, key: str) -> bool:
        """True when we have a manifest record *and* the file is still there."""
        return key in self.entries and self.file_exists(key)

    def forget_missing(self) -> int:
        gone = [k for k in self.entries if not self.file_exists(k)]
        for key in gone:
            del self.entries[key]
        return len(gone)
