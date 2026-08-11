"""MkDocs Material sites (Kuadrant, Open Data Hub Models-as-a-Service).

These publish no PDFs, so pages are rendered and merged. Versions come from the
`mike` versions.json, pages from sitemap.xml, and reading order from the nav on
the landing page.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse

from ..http_client import HttpError
from ..util import (
    LOG,
    clean_segment,
    compile_patterns,
    dedupe,
    matches_any,
    pick_latest,
    safe_filename,
)
from .base import Doc, Source, make_soup

SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


class MkDocsSource(Source):
    kind = "mkdocs"

    def __init__(
        self,
        id: str,
        label: str,
        root_url: str,
        version: str = "latest",
        mode: str = "single",           # single | per-page
        max_pages: int = 600,
        include: Optional[Sequence[str]] = None,
        exclude: Optional[Sequence[str]] = None,
        fingerprint: bool = True,
        community: bool = True,
    ) -> None:
        self.id = id
        self.label = label
        self.root_url = root_url.rstrip("/")
        self.requested_version = version or "latest"
        self.mode = mode
        self.max_pages = max_pages
        self.include = compile_patterns(include)
        self.exclude = compile_patterns(exclude)
        self.fingerprint = fingerprint
        self.community = community

    # -- versions ---------------------------------------------------------
    def resolve_version(self, http) -> str:
        requested = self.requested_version
        url = f"{self.root_url}/versions.json"
        try:
            text, _ = http.get_text(url)
            entries = json.loads(text)
        except (HttpError, json.JSONDecodeError, ValueError) as exc:
            LOG.debug("[%s] no usable versions.json (%s)", self.id, exc)
            return requested

        versions, aliases = [], {}
        for entry in entries if isinstance(entries, list) else []:
            version = str(entry.get("version", "")).strip()
            if not version:
                continue
            versions.append(version)
            for alias in entry.get("aliases", []) or []:
                aliases[str(alias)] = version

        if requested in versions:
            return requested
        if requested in aliases:
            LOG.debug("[%s] alias %s -> %s", self.id, requested, aliases[requested])
            return requested  # keep the alias in URLs; it is a stable entry point
        if requested in ("latest", "stable", "current"):
            resolved = aliases.get(requested) or pick_latest(versions)
            if resolved:
                LOG.debug("[%s] %s resolves to %s", self.id, requested, resolved)
                return requested if requested in aliases else resolved
        LOG.warning("[%s] version %s not listed; available: %s", self.id, requested, ", ".join(versions[:8]))
        return requested

    def base_url(self, version: str) -> str:
        return f"{self.root_url}/{version}"

    def display_version(self, http, version: str) -> str:
        """The canonical link exposes the concrete version behind an alias."""
        try:
            html, _ = http.get_text(f"{self.base_url(version)}/")
        except HttpError:
            return version
        soup = make_soup(html)
        link = soup.find("link", rel="canonical")
        if link and link.get("href"):
            match = re.search(r"/([^/]+)/?$", urlparse(link["href"]).path.rstrip("/"))
            path_parts = [p for p in urlparse(link["href"]).path.split("/") if p]
            root_parts = [p for p in urlparse(self.root_url).path.split("/") if p]
            if len(path_parts) > len(root_parts):
                candidate = path_parts[len(root_parts)]
                if candidate and candidate != version:
                    return candidate
            if match:
                return match.group(1)
        return version

    # -- pages ------------------------------------------------------------
    def discover(self, http, workers: int = 8) -> List[Doc]:
        version = self.resolve_version(http)
        base = self.base_url(version)
        concrete = self.display_version(http, version)
        LOG.info("[%s] %s %s%s", self.id, self.label, version,
                 f" (build {concrete})" if concrete != version else "")

        pages = self._pages_from_sitemap(http, base)
        ordered = self._nav_order(http, base)
        if not pages:
            LOG.warning("[%s] sitemap empty; falling back to navigation links", self.id)
            pages = ordered
        if not pages:
            LOG.error("[%s] no pages discovered at %s", self.id, base)
            return []

        pages = self._sort_pages(pages, ordered)
        pages = [p for p in pages if self._wanted(p)][: self.max_pages]
        titles = [self._title_for(p, base) for p in pages]
        LOG.info("[%s] %d pages", self.id, len(pages))

        if self.mode == "per-page":
            docs = []
            for url, title in zip(pages, titles):
                slug = self._slug(url, base) or "index"
                docs.append(
                    Doc(
                        key=f"mkdocs:{self.id}:{version}:{slug}",
                        source_id=self.id,
                        product=self.label,
                        version=version,
                        title=title,
                        filename=safe_filename(f"{clean_segment(title)}.pdf"),
                        rel_dir=f"{self.id}/{version}/{'/'.join(slug.split('/')[:-1])}".rstrip("/"),
                        page_url=url,
                        render_urls=[url],
                        render_titles=[title],
                        fingerprint=http.fingerprint(url) if self.fingerprint else None,
                        note="converted from HTML",
                    )
                )
            return docs

        fingerprint = self._site_fingerprint(http, pages, workers) if self.fingerprint else None
        name = safe_filename(f"{clean_segment(self.label)}-{clean_segment(version)}-Documentation-en-US.pdf")
        return [
            Doc(
                key=f"mkdocs:{self.id}:{version}:site",
                source_id=self.id,
                product=self.label,
                version=version,
                title=f"{self.label} documentation",
                filename=name,
                rel_dir=f"{self.id}/{version}",
                page_url=f"{base}/",
                render_urls=pages,
                render_titles=titles,
                fingerprint=fingerprint,
                note=f"converted from {len(pages)} HTML pages",
            )
        ]

    def _pages_from_sitemap(self, http, base: str) -> List[str]:
        for candidate in (f"{base}/sitemap.xml", f"{self.root_url}/sitemap.xml"):
            try:
                text, _ = http.get_text(candidate)
                root = ET.fromstring(text.encode("utf-8", "ignore"))
            except (HttpError, ET.ParseError) as exc:
                LOG.debug("[%s] sitemap %s unusable (%s)", self.id, candidate, exc)
                continue
            locs = [el.text.strip() for el in root.iterfind(".//sm:url/sm:loc", SITEMAP_NS) if el.text]
            if not locs:
                locs = [el.text.strip() for el in root.iter() if el.tag.endswith("loc") and el.text]
            pages = [u for u in locs if u.startswith(base + "/") or u.rstrip("/") == base]
            if pages:
                return dedupe(pages)
        return []

    def _nav_order(self, http, base: str) -> List[str]:
        try:
            html, final_url = http.get_text(f"{base}/")
        except HttpError as exc:
            LOG.debug("[%s] landing page unavailable (%s)", self.id, exc)
            return []
        soup = make_soup(html)
        containers = soup.select("nav.md-nav--primary") or soup.select("nav") or [soup]
        urls: List[str] = []
        for container in containers:
            for anchor in container.find_all("a", href=True):
                url = urljoin(final_url, anchor["href"]).split("#")[0]
                if url.startswith(base + "/") or url.rstrip("/") == base:
                    urls.append(url)
        return dedupe(urls)

    @staticmethod
    def _sort_pages(pages: List[str], order: List[str]) -> List[str]:
        rank = {url.rstrip("/"): i for i, url in enumerate(order)}
        return sorted(pages, key=lambda u: (rank.get(u.rstrip("/"), len(rank)), u))

    def _wanted(self, url: str) -> bool:
        if self.include and not matches_any(url, self.include):
            return False
        return not matches_any(url, self.exclude)

    @staticmethod
    def _slug(url: str, base: str) -> str:
        rel = url[len(base):].strip("/") if url.startswith(base) else urlparse(url).path.strip("/")
        return rel or "index"

    def _title_for(self, url: str, base: str) -> str:
        slug = self._slug(url, base)
        last = [p for p in slug.split("/") if p]
        name = last[-1] if last else "index"
        return name.replace("-", " ").replace("_", " ").strip().title() or "Home"

    def _site_fingerprint(self, http, pages: Sequence[str], workers: int) -> str:
        import hashlib

        digest = hashlib.sha256()
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for url, tag in zip(pages, pool.map(http.fingerprint, pages)):
                digest.update(url.encode("utf-8"))
                digest.update((tag or "").encode("utf-8"))
        return digest.hexdigest()
