"""Renderer selection - regression tests. No network, no real browser.

Covers the crash where a browser opened for the 403 fallback caused a second
sync Playwright instance to be started in the same thread:

    playwright._impl._errors.Error: It looks like you are using Playwright Sync
    API inside the asyncio loop. Please use the Async API instead.

Run with: python tests/test_render_selection.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docsync.downloader import Downloader, Options
from docsync.manifest import Manifest
from docsync.pdf_render import (
    BaseRenderer,
    RenderError,
    SharedBrowserRenderer,
    get_renderer,
    iter_renderers,
)
from docsync.sources.base import Doc
from docsync.util import setup_logging

MINIMAL_PDF = b"%PDF-1.7\n1 0 obj<</Type/Catalog>>endobj\ntrailer\n%%EOF\n"


class FakeSession:
    """Stands in for a BrowserSession already opened by the HTTP client."""

    def __init__(self, backend="playwright"):
        self.backend = backend
        self.started = 0
        self.stopped = 0
        self.printed = []

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def print_pdf(self, url, dest, css="", wait=1.5):
        self.printed.append(url)
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(MINIMAL_PDF)


class FakeHttp:
    def __init__(self, session=None):
        self._session = session

    def active_browser(self):
        return self._session


class ExplodingRenderer(BaseRenderer):
    name = "exploding"

    def available(self):
        return True

    def start(self):
        raise RenderError("simulated: Sync API inside the asyncio loop")


class WorkingRenderer(BaseRenderer):
    name = "working"

    def __init__(self):
        self.started = False

    def available(self):
        return True

    def start(self):
        self.started = True

    def render(self, url, dest, wait=1.5):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(MINIMAL_PDF)
        return dest


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}{('  ' + str(detail)) if not condition else ''}")
    return bool(condition)


def main():
    setup_logging(quiet=True)
    ok = True

    # --- the regression itself -------------------------------------------
    session = FakeSession("playwright")
    first = next(iter_renderers("auto", FakeHttp(session)), None)
    ok &= check("an open browser is reused instead of starting a second one",
                isinstance(first, SharedBrowserRenderer), type(first).__name__)

    renderer = get_renderer("auto", FakeHttp(session))
    ok &= check("get_renderer returns the shared renderer, started once",
                isinstance(renderer, SharedBrowserRenderer) and session.started == 1, session.started)
    renderer.stop()
    ok &= check("the shared session is not closed by the renderer", session.stopped == 0)

    # With no browser open, selection falls through to the normal engines.
    plain = list(iter_renderers("auto", FakeHttp(None)))
    ok &= check("no shared renderer when no browser is open",
                not any(isinstance(r, SharedBrowserRenderer) for r in plain))

    # --- an engine that fails to start must not kill the run --------------
    order = [ExplodingRenderer(), WorkingRenderer()]
    picked = None
    for candidate in order:
        try:
            candidate.start()
            picked = candidate
            break
        except Exception:
            continue
    ok &= check("selection moves on when an engine refuses to start",
                isinstance(picked, WorkingRenderer))

    # --- end to end through the downloader --------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        session = FakeSession("playwright")
        doc = Doc(
            key="mkdocs:test:1.0:site", source_id="test", product="Test Docs", version="1.0",
            title="Test Docs", filename="Test-Docs.pdf", rel_dir="test/1.0",
            page_url="https://example.invalid/", pdf_url=None, pdf_candidates=[],
            render_urls=["https://example.invalid/"], render_titles=["Home"],
            fingerprint="fp-1",
        )
        options = Options(out_dir=out, workers=2, convert=True)
        results = Downloader(FakeHttp(session), Manifest(out), options).run([doc])
        result = results[0]
        ok &= check("document with no PDF is converted", result.status == "converted", result.message)
        ok &= check("rendered file written", (out / "test/1.0/Test-Docs.pdf").exists())
        ok &= check("the shared browser did the rendering", session.printed == ["https://example.invalid/"])

        # second run: the fingerprint is unchanged, so nothing is re-rendered
        session2 = FakeSession("playwright")
        again = Downloader(FakeHttp(session2), Manifest(out), options).run([doc])
        ok &= check("unchanged fingerprint skips re-rendering",
                    again[0].status == "unchanged" and not session2.printed, again[0].status)

    print("\nALL PASSED" if ok else "\nSOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
