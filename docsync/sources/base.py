"""Common types shared by every source."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from bs4 import BeautifulSoup


@dataclass
class Doc:
    """One downloadable document."""

    key: str                      # stable identity across runs
    source_id: str
    product: str                  # display name, e.g. "Red Hat OpenShift AI Self-Managed"
    version: str
    title: str
    filename: str                 # target file name, including .pdf
    rel_dir: str = ""             # sub-directory under the output root
    page_url: str = ""            # human-readable landing page
    pdf_url: Optional[str] = None  # verified or candidate direct PDF
    pdf_candidates: List[str] = field(default_factory=list)
    render_urls: List[str] = field(default_factory=list)  # used when converting
    render_titles: List[str] = field(default_factory=list)
    fingerprint: Optional[str] = None  # change signal for converted documents
    category: str = ""                 # section of the product index, e.g. "Networking"
    note: str = ""

    @property
    def label(self) -> str:
        return f"{self.product} {self.version} - {self.title}"


class Source:
    """A place documents come from."""

    id: str = "source"
    label: str = "Source"
    kind: str = "generic"
    community: bool = False

    def discover(self, http, workers: int = 8) -> List[Doc]:
        raise NotImplementedError

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"{self.id} ({self.label})"


def make_soup(html: str) -> BeautifulSoup:
    """Prefer lxml when it is installed, fall back to the stdlib parser."""
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")
