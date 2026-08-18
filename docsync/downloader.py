"""Turns discovered documents into files on disk."""

from __future__ import annotations

import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .http_client import HttpClient, sha256_file
from .manifest import Entry, Manifest
from .pdf_render import RenderError, get_renderer, merge_pdfs
from .sources.base import Doc
from .util import LOG, human_size

DOWNLOADED = "downloaded"
CONVERTED = "converted"
UNCHANGED = "unchanged"
SKIPPED = "skipped"
FAILED = "failed"
PLANNED = "planned"


@dataclass
class Result:
    doc: Doc
    status: str
    path: Optional[Path] = None
    message: str = ""
    size: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.status in (DOWNLOADED, CONVERTED, UNCHANGED, SKIPPED, PLANNED)


@dataclass
class Options:
    out_dir: Path = Path("docs")
    only_new: bool = False
    force: bool = False
    convert: bool = True
    dry_run: bool = False
    flat: bool = False
    workers: int = 6
    pdf_engine: str = "auto"
    render_wait: float = 1.5
    keep_parts: bool = False
    results: List[Result] = field(default_factory=list)


class Downloader:
    def __init__(self, http: HttpClient, manifest: Manifest, options: Options) -> None:
        self.http = http
        self.manifest = manifest
        self.options = options
        self._lock = threading.Lock()
        self._renderer = None

    # -- helpers ----------------------------------------------------------
    def target_path(self, doc: Doc) -> Path:
        if self.options.flat or not doc.rel_dir:
            return self.options.out_dir / doc.filename
        return self.options.out_dir / doc.rel_dir / doc.filename

    def _record(self, doc: Doc, path: Path, method: str, *, size=None, sha256=None,
                etag=None, last_modified=None, url="", fingerprint=None) -> None:
        try:
            rel = str(path.relative_to(self.options.out_dir))
        except ValueError:
            rel = str(path)
        entry = Entry(
            key=doc.key,
            source=doc.source_id,
            product=doc.product,
            doc_version=doc.version,
            title=doc.title,
            url=url or doc.pdf_url or doc.page_url,
            path=rel,
            method=method,
            size=size,
            sha256=sha256,
            etag=etag,
            last_modified=last_modified,
            fingerprint=fingerprint if fingerprint is not None else doc.fingerprint,
        )
        with self._lock:
            self.manifest.put(entry)

    # -- main entry point --------------------------------------------------
    def run(self, docs: Sequence[Doc]) -> List[Result]:
        results: List[Result] = []
        to_convert: List[Doc] = []

        pending = []
        for doc in docs:
            decision = self._precheck(doc)
            if decision is not None:
                results.append(decision)
            else:
                pending.append(doc)

        if pending:
            with ThreadPoolExecutor(max_workers=max(1, self.options.workers)) as pool:
                futures = {pool.submit(self._fetch_pdf, doc): doc for doc in pending}
                for future in as_completed(futures):
                    doc = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:  # never let one document kill the run
                        result = Result(doc, FAILED, message=f"unexpected error: {exc}")
                    if result is None:
                        to_convert.append(doc)
                    else:
                        results.append(result)
                        self._log_result(result)

        if to_convert:
            try:
                results.extend(self._convert_all(to_convert))
            except Exception as exc:  # keep whatever already downloaded
                LOG.error("conversion stage failed: %s", exc)
                LOG.debug("", exc_info=True)
                done = {id(r.doc) for r in results}
                for doc in to_convert:
                    if id(doc) not in done:
                        results.append(Result(doc, FAILED, message=f"conversion stage failed: {exc}"))

        self._save_manifest()
        results.sort(key=lambda r: (r.doc.source_id, r.doc.product, r.doc.version, r.doc.title))
        return results

    # -- phase 1: is there anything to do at all? --------------------------
    def _precheck(self, doc: Doc) -> Optional[Result]:
        path = self.target_path(doc)
        entry = self.manifest.get(doc.key)

        if self.options.force:
            return None

        if self.options.only_new:
            if self.manifest.known(doc.key) or path.exists():
                return Result(doc, SKIPPED, path=path, message="already present")
            return None

        # Converted documents: compare the fingerprint we computed during discovery.
        if doc.fingerprint and entry and entry.fingerprint == doc.fingerprint and path.exists():
            return Result(doc, UNCHANGED, path=path, message="fingerprint unchanged")

        return None

    # -- phase 2: direct PDF ----------------------------------------------
    def _fetch_pdf(self, doc: Doc) -> Optional[Result]:
        """Returns a Result, or None when the document must be converted."""
        path = self.target_path(doc)
        entry = self.manifest.get(doc.key)

        candidates = list(doc.pdf_candidates or ([doc.pdf_url] if doc.pdf_url else []))
        if entry and entry.url and entry.method == "pdf" and entry.url in candidates:
            candidates.remove(entry.url)
            candidates.insert(0, entry.url)

        verified = None
        reasons = []
        for candidate in candidates:
            probe = self.http.probe_pdf(candidate)
            if probe.ok:
                verified = probe
                doc.pdf_url = candidate
                break
            reasons.append(f"{candidate.rsplit('/', 1)[-1]}: {probe.reason or probe.status}")

        if not verified:
            if candidates:
                LOG.debug("[%s] no published PDF for %s (%s)", doc.source_id, doc.title, "; ".join(reasons[:3]))
            if self.options.convert and doc.render_urls:
                return None
            return Result(doc, FAILED, message="no PDF found and conversion is disabled")

        if self.options.dry_run:
            return Result(doc, PLANNED, path=path, message=f"would download {doc.pdf_url}",
                          size=verified.length)

        etag = entry.etag if (entry and path.exists() and not self.options.force) else None
        modified = entry.last_modified if (entry and path.exists() and not self.options.force) else None

        result = self.http.download(doc.pdf_url, path, expect_pdf=True, etag=etag, last_modified=modified)
        if result.status == "unchanged":
            self._record(doc, path, "pdf", size=entry.size if entry else None,
                         sha256=entry.sha256 if entry else None, etag=etag,
                         last_modified=modified, url=doc.pdf_url)
            return Result(doc, UNCHANGED, path=path, message="not modified since last run")
        if result.status == "downloaded":
            self._record(doc, path, "pdf", size=result.size, sha256=result.sha256,
                         etag=result.etag, last_modified=result.last_modified, url=doc.pdf_url)
            return Result(doc, DOWNLOADED, path=path, size=result.size)

        LOG.debug("[%s] download failed for %s: %s", doc.source_id, doc.title, result.error)
        if self.options.convert and doc.render_urls:
            return None
        return Result(doc, FAILED, message=result.error or "download failed")

    # -- phase 3: conversion ----------------------------------------------
    def _convert_all(self, docs: List[Doc]) -> List[Result]:
        results: List[Result] = []
        if self.options.dry_run:
            for doc in docs:
                pages = len(doc.render_urls)
                results.append(
                    Result(doc, PLANNED, path=self.target_path(doc),
                           message=f"would convert {pages} page(s) to PDF")
                )
                self._log_result(results[-1])
            return results

        renderer = get_renderer(self.options.pdf_engine, http=self.http)
        if renderer is None:
            for doc in docs:
                results.append(Result(doc, FAILED, message="no PDF available and no rendering engine installed"))
                self._log_result(results[-1])
            return results

        LOG.info("converting %d document(s) with %s", len(docs), renderer.name)
        try:
            for doc in docs:
                results.append(self._convert_one(renderer, doc))
                self._log_result(results[-1])
                self._save_manifest()
        finally:
            renderer.stop()
        return results

    def _convert_one(self, renderer, doc: Doc) -> Result:
        path = self.target_path(doc)
        urls = doc.render_urls or ([doc.page_url] if doc.page_url else [])
        if not urls:
            return Result(doc, FAILED, message="nothing to render")

        workdir = Path(tempfile.mkdtemp(prefix="docsync-render-"))
        parts: List[Path] = []
        titles: List[str] = []
        failures = 0
        try:
            for index, url in enumerate(urls):
                part = workdir / f"{index:04d}.pdf"
                label = doc.render_titles[index] if index < len(doc.render_titles) else f"Page {index + 1}"
                try:
                    renderer.render(url, part, wait=self.options.render_wait)
                    parts.append(part)
                    titles.append(label)
                except RenderError as exc:
                    failures += 1
                    LOG.warning("[%s] could not render %s (%s)", doc.source_id, url, exc)
                if len(urls) > 8 and index and index % 10 == 0:
                    LOG.info("[%s]   %d/%d pages rendered", doc.source_id, index, len(urls))

            if not parts:
                return Result(doc, FAILED, message="every page failed to render")

            path.parent.mkdir(parents=True, exist_ok=True)
            if len(parts) == 1:
                shutil.copyfile(parts[0], path)
            else:
                merge_pdfs(parts, path, titles=titles, doc_title=f"{doc.product} {doc.version} - {doc.title}")

            size = path.stat().st_size
            self._record(doc, path, "converted", size=size, sha256=sha256_file(path),
                         url=doc.page_url, fingerprint=doc.fingerprint)
            note = f"{len(parts)} page(s)"
            if failures:
                note += f", {failures} failed"
            return Result(doc, CONVERTED, path=path, size=size, message=note)
        except RenderError as exc:
            return Result(doc, FAILED, message=str(exc))
        finally:
            if not self.options.keep_parts:
                shutil.rmtree(workdir, ignore_errors=True)

    # -- reporting ---------------------------------------------------------
    def _log_result(self, result: Result) -> None:
        doc = result.doc
        name = result.path.name if result.path else doc.filename
        if result.status == DOWNLOADED:
            LOG.info("  [ok]      %s (%s)", name, human_size(result.size))
        elif result.status == CONVERTED:
            LOG.info("  [convert] %s (%s, %s)", name, human_size(result.size), result.message)
        elif result.status == UNCHANGED:
            LOG.info("  [same]    %s", name)
        elif result.status == SKIPPED:
            LOG.debug("  [skip]    %s - %s", name, result.message)
        elif result.status == PLANNED:
            LOG.info("  [plan]    %s - %s", name, result.message)
        else:
            LOG.warning("  [fail]    %s - %s", doc.label, result.message)

    def _save_manifest(self) -> None:
        if self.options.dry_run:
            return
        try:
            with self._lock:
                self.manifest.save()
        except OSError as exc:
            LOG.warning("could not write manifest: %s", exc)


def summarise(results: Sequence[Result]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts
