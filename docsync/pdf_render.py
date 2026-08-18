"""Render HTML pages to PDF when a product does not publish one.

Four back ends are supported, tried in this order when --pdf-engine=auto:

  playwright  - best fidelity, handles JavaScript-rendered pages
  selenium    - headless Chrome via CDP Page.printToPDF (same stack the
                original script already used)
  weasyprint  - no browser needed, but no JavaScript either
  wkhtmltopdf - external binary, last resort

Only one renderer is started per run and it is used sequentially, which keeps
browser handling simple and predictable.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Sequence
from urllib.parse import urlparse

from .util import LOG

# Chrome/Playwright printing keeps the sidebars and headers unless we hide them.
GENERIC_PRINT_CSS = """
@page { size: A4; margin: 14mm 12mm; }
html, body { background: #fff !important; }
video, iframe, .md-dialog, .md-banner, [data-md-component="announce"] { display: none !important; }
a { text-decoration: none !important; color: inherit !important; }
pre, code { white-space: pre-wrap !important; word-break: break-word !important; }
img, svg, table { max-width: 100% !important; page-break-inside: avoid; }
h1, h2, h3 { page-break-after: avoid; }
"""

MKDOCS_PRINT_CSS = """
.md-header, .md-tabs, .md-sidebar, .md-nav, .md-footer, .md-search,
.md-top, .md-source, .md-content__button, .md-skip { display: none !important; }
.md-main__inner, .md-content, .md-grid { margin: 0 !important; max-width: 100% !important; }
.md-content__inner { padding: 0 !important; }
"""

REDHAT_PRINT_CSS = """
header, footer, nav, .pf-v5-c-page__sidebar, .pf-c-page__sidebar,
[data-testid="toc"], [class*="Toc"], [class*="toc"], [class*="Breadcrumb"],
[class*="page-settings"], [class*="Feedback"], [class*="feedback"],
.rh-footer, #pfe-navigation, .pfe-navigation { display: none !important; }
main, [role="main"], .pf-v5-c-page__main, .pf-c-page__main {
  margin: 0 !important; padding: 0 !important; width: 100% !important; max-width: 100% !important;
}
"""


def print_css_for(url: str) -> str:
    host = urlparse(url).netloc.lower()
    css = GENERIC_PRINT_CSS
    if "docs.redhat.com" in host or "access.redhat.com" in host:
        css += REDHAT_PRINT_CSS
    else:
        # opendatahub-io.github.io and docs.kuadrant.io are both MkDocs Material.
        css += MKDOCS_PRINT_CSS
    return css


class RenderError(Exception):
    pass


class BaseRenderer:
    name = "base"

    def available(self) -> bool:
        raise NotImplementedError

    def start(self) -> None:
        pass

    def render(self, url: str, dest: Path, wait: float = 1.5) -> Path:
        raise NotImplementedError

    def stop(self) -> None:
        pass

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False


class PlaywrightRenderer(BaseRenderer):
    name = "playwright"

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._context = None

    def available(self) -> bool:
        try:
            import playwright.sync_api  # noqa: F401
            return True
        except ImportError:
            return False

    def start(self) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        self._context = self._browser.new_context(viewport={"width": 1280, "height": 1600})

    def render(self, url: str, dest: Path, wait: float = 1.5) -> Path:
        if self._context is None:
            raise RenderError("playwright renderer was not started")
        page = self._context.new_page()
        try:
            page.goto(url, wait_until="load", timeout=90_000)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass  # networkidle never settles on some analytics-heavy pages
            page.add_style_tag(content=print_css_for(url))
            page.emulate_media(media="screen")
            time.sleep(wait)
            dest.parent.mkdir(parents=True, exist_ok=True)
            page.pdf(
                path=str(dest),
                format="A4",
                print_background=True,
                margin={"top": "14mm", "bottom": "14mm", "left": "12mm", "right": "12mm"},
            )
            return dest
        except Exception as exc:
            raise RenderError(f"playwright: {exc}") from exc
        finally:
            page.close()

    def stop(self) -> None:
        for obj in (self._context, self._browser):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass


class SeleniumRenderer(BaseRenderer):
    """Headless Chrome driven through CDP Page.printToPDF."""

    name = "selenium"

    def __init__(self, driver_path: Optional[str] = None) -> None:
        self.driver = None
        self.driver_path = driver_path

    def available(self) -> bool:
        try:
            import selenium  # noqa: F401
            return True
        except ImportError:
            return False

    def start(self) -> None:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1280,1600")
        options.add_argument("--hide-scrollbars")

        service = None
        if self.driver_path:
            service = Service(self.driver_path)
        else:
            try:
                from webdriver_manager.chrome import ChromeDriverManager

                service = Service(ChromeDriverManager().install())
            except Exception as exc:  # Selenium Manager (>=4.6) can still find a driver.
                LOG.debug("webdriver_manager unavailable (%s); relying on Selenium Manager", exc)
        try:
            self.driver = webdriver.Chrome(service=service, options=options) if service \
                else webdriver.Chrome(options=options)
        except Exception as exc:
            raise RenderError(f"could not start headless Chrome: {exc}") from exc
        self.driver.set_page_load_timeout(90)

    def render(self, url: str, dest: Path, wait: float = 1.5) -> Path:
        if self.driver is None:
            raise RenderError("selenium renderer was not started")
        try:
            self.driver.get(url)
            deadline = time.time() + 30
            while time.time() < deadline:
                if self.driver.execute_script("return document.readyState") == "complete":
                    break
                time.sleep(0.25)
            time.sleep(wait)
            self.driver.execute_script(
                "var s=document.createElement('style');s.setAttribute('data-docsync','1');"
                "s.textContent=arguments[0];document.head.appendChild(s);",
                print_css_for(url),
            )
            result = self.driver.execute_cdp_cmd(
                "Page.printToPDF",
                {
                    "printBackground": True,
                    "preferCSSPageSize": True,
                    "paperWidth": 8.27,
                    "paperHeight": 11.69,
                    "marginTop": 0.55,
                    "marginBottom": 0.55,
                    "marginLeft": 0.47,
                    "marginRight": 0.47,
                    "transferMode": "ReturnAsBase64",
                },
            )
            data = base64.b64decode(result["data"])
            if not data.startswith(b"%PDF-"):
                raise RenderError("Chrome returned something that is not a PDF")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return dest
        except RenderError:
            raise
        except Exception as exc:
            raise RenderError(f"selenium: {exc}") from exc

    def stop(self) -> None:
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass


class WeasyPrintRenderer(BaseRenderer):
    """No browser required, but JavaScript-rendered content will be missing."""

    name = "weasyprint"

    def __init__(self, http=None) -> None:
        self.http = http

    def available(self) -> bool:
        try:
            import weasyprint  # noqa: F401
            return True
        except Exception:
            return False

    def render(self, url: str, dest: Path, wait: float = 0.0) -> Path:
        from weasyprint import CSS, HTML

        try:
            if self.http is not None:
                html, final_url = self.http.get_text(url)
                doc = HTML(string=html, base_url=final_url)
            else:
                doc = HTML(url=url)
            dest.parent.mkdir(parents=True, exist_ok=True)
            doc.write_pdf(str(dest), stylesheets=[CSS(string=print_css_for(url))])
            return dest
        except Exception as exc:
            raise RenderError(f"weasyprint: {exc}") from exc


class WkhtmltopdfRenderer(BaseRenderer):
    name = "wkhtmltopdf"

    def available(self) -> bool:
        return shutil.which("wkhtmltopdf") is not None

    def render(self, url: str, dest: Path, wait: float = 1.5) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", suffix=".css", delete=False, encoding="utf-8") as css_file:
            css_file.write(print_css_for(url))
            css_path = css_file.name
        cmd = [
            "wkhtmltopdf", "--quiet", "--enable-local-file-access",
            "--javascript-delay", str(int(wait * 1000)),
            "--user-style-sheet", css_path,
            "--page-size", "A4", url, str(dest),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=180)
            if proc.returncode != 0 or not dest.exists():
                raise RenderError(f"wkhtmltopdf exited {proc.returncode}: {proc.stderr.decode()[:200]}")
            return dest
        except subprocess.TimeoutExpired as exc:
            raise RenderError("wkhtmltopdf timed out") from exc
        finally:
            Path(css_path).unlink(missing_ok=True)


class SharedBrowserRenderer(BaseRenderer):
    """Renders through a BrowserSession that somebody else owns.

    When a 403 forced docsync to open headless Chrome, that instance must be
    reused: Playwright's sync API raises "Sync API inside the asyncio loop" if
    a second instance is started in the same thread, and a second Selenium
    driver would just waste memory.
    """

    def __init__(self, session) -> None:
        self.session = session
        self.name = f"{getattr(session, 'backend', None) or 'browser'} (shared)"

    def available(self) -> bool:
        return self.session is not None

    def start(self) -> None:
        self.session.start()

    def render(self, url: str, dest: Path, wait: float = 1.5) -> Path:
        try:
            self.session.print_pdf(url, dest, css=print_css_for(url), wait=wait)
            return dest
        except Exception as exc:
            raise RenderError(f"{self.name}: {exc}") from exc

    def stop(self) -> None:
        pass  # the HTTP client owns this session and closes it


ENGINES = {
    "playwright": PlaywrightRenderer,
    "selenium": SeleniumRenderer,
    "weasyprint": WeasyPrintRenderer,
    "wkhtmltopdf": WkhtmltopdfRenderer,
}
AUTO_ORDER = ["playwright", "selenium", "weasyprint", "wkhtmltopdf"]


def get_renderer(preference: str = "auto", http=None) -> Optional[BaseRenderer]:
    """Return a renderer that has actually started, or None if none can."""
    for renderer in iter_renderers(preference, http):
        try:
            renderer.start()
            LOG.debug("using %s for HTML to PDF conversion", renderer.name)
            return renderer
        except Exception as exc:
            LOG.warning("%s could not start (%s); trying the next engine", renderer.name, exc)
            try:
                renderer.stop()
            except Exception:
                pass
    LOG.warning(
        "no usable HTML to PDF engine - install one of: "
        "'pip install playwright && playwright install chromium', selenium + Chrome, or weasyprint"
    )
    return None


def iter_renderers(preference: str = "auto", http=None):
    """Yield candidate renderers, best first, without starting them."""
    if preference == "none":
        return

    # A browser opened for the 403 fallback is reused rather than duplicated.
    session = getattr(http, "active_browser", lambda: None)()
    if session is not None and preference in ("auto", "playwright", "selenium", "browser"):
        backend = getattr(session, "backend", None)
        if preference in ("auto", "browser") or backend in (None, preference):
            yield SharedBrowserRenderer(session)

    order = AUTO_ORDER if preference == "auto" else [preference]
    for name in order:
        cls = ENGINES.get(name)
        if cls is None:
            LOG.warning("unknown PDF engine %r", name)
            return
        renderer = cls(http=http) if name == "weasyprint" else cls()
        if renderer.available():
            yield renderer
        elif preference != "auto":
            LOG.warning("PDF engine %r is not installed or not usable", name)
            return


def merge_pdfs(parts: Sequence[Path], dest: Path, titles: Optional[Sequence[str]] = None,
               doc_title: Optional[str] = None) -> Path:
    """Concatenate rendered pages into one PDF with a bookmark per page."""
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:  # pragma: no cover
        raise RenderError("pypdf is required to merge PDFs (pip install pypdf)") from exc

    writer = PdfWriter()
    labels: List[str] = list(titles or [])
    added = 0
    for idx, part in enumerate(parts):
        try:
            reader = PdfReader(str(part))
        except Exception as exc:
            LOG.warning("skipping unreadable part %s (%s)", part.name, exc)
            continue
        if not reader.pages:
            continue
        start = len(writer.pages)
        for page in reader.pages:
            writer.add_page(page)
        label = labels[idx] if idx < len(labels) else part.stem
        try:
            writer.add_outline_item(label[:120], start)
        except Exception:
            pass
        added += 1

    if not added:
        raise RenderError("nothing to merge - every rendered part failed")

    if doc_title:
        try:
            writer.add_metadata({"/Title": doc_title, "/Producer": "docsync"})
        except Exception:
            pass

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    with open(tmp, "wb") as handle:
        writer.write(handle)
    tmp.replace(dest)
    return dest
