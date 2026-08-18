"""A real browser, used only when plain HTTP is refused.

Sites behind Akamai / Cloudflare sometimes answer a scripted request with 403
even though the same URL opens fine in a browser. When that happens docsync can

  1. open the site once in headless Chrome,
  2. copy the resulting cookies and User-Agent into the requests session, and
  3. retry - which usually works, because the bot check has now been passed.

If even that is refused, the page HTML (and, if needed, the PDF bytes) can be
pulled straight out of the browser.
"""

from __future__ import annotations

import base64
import time
from typing import Dict, List, Optional

from .util import LOG


class BrowserUnavailable(Exception):
    pass


class BrowserSession:
    """Thin wrapper over Playwright or Selenium, whichever is installed."""

    def __init__(self, engine: str = "auto", headless: bool = True) -> None:
        self.engine = engine
        self.headless = headless
        self.backend: Optional[str] = None
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._driver = None

    # -- lifecycle --------------------------------------------------------
    @staticmethod
    def _has(module: str) -> bool:
        try:
            __import__(module)
            return True
        except ImportError:
            return False

    def available(self) -> bool:
        if self.engine in ("playwright", "auto") and self._has("playwright"):
            return True
        if self.engine in ("selenium", "auto") and self._has("selenium"):
            return True
        return False

    def start(self) -> None:
        if self.backend:
            return
        order = ["playwright", "selenium"] if self.engine == "auto" else [self.engine]
        errors: List[str] = []
        for name in order:
            try:
                if name == "playwright" and self._has("playwright"):
                    self._start_playwright()
                    self.backend = "playwright"
                    return
                if name == "selenium" and self._has("selenium"):
                    self._start_selenium()
                    self.backend = "selenium"
                    return
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        raise BrowserUnavailable("; ".join(errors) or "no browser back end installed")

    def _start_playwright(self) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        self._context = self._browser.new_context(
            viewport={"width": 1280, "height": 1600},
            locale="en-US",
        )
        self._page = self._context.new_page()

    def _start_selenium(self) -> None:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        options = Options()
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1280,1600")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--lang=en-US")

        service = None
        try:
            from webdriver_manager.chrome import ChromeDriverManager

            service = Service(ChromeDriverManager().install())
        except Exception as exc:
            LOG.debug("webdriver_manager unavailable (%s); relying on Selenium Manager", exc)
        self._driver = webdriver.Chrome(service=service, options=options) if service \
            else webdriver.Chrome(options=options)
        self._driver.set_page_load_timeout(90)

    def stop(self) -> None:
        for obj in (self._page, self._context, self._browser):
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
        try:
            if self._driver:
                self._driver.quit()
        except Exception:
            pass
        self.backend = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    # -- use --------------------------------------------------------------
    def visit(self, url: str, wait: float = 2.0) -> None:
        self.start()
        if self.backend == "playwright":
            self._page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        else:
            self._driver.get(url)
        time.sleep(wait)

    def get_html(self, url: str, wait: float = 2.0) -> str:
        self.visit(url, wait=wait)
        return self._page.content() if self.backend == "playwright" else self._driver.page_source

    def cookies(self) -> Dict[str, str]:
        self.start()
        if self.backend == "playwright":
            return {c["name"]: c["value"] for c in self._context.cookies()}
        return {c["name"]: c["value"] for c in self._driver.get_cookies()}

    def user_agent(self) -> str:
        self.start()
        script = "return navigator.userAgent"
        if self.backend == "playwright":
            return self._page.evaluate("() => navigator.userAgent")
        return self._driver.execute_script(script)

    def print_pdf(self, url: str, dest, css: str = "", wait: float = 1.5) -> None:
        """Print a page to PDF using this already-running browser.

        Sharing the session matters: Playwright's sync API refuses to start a
        second instance in the same thread, so a browser opened for the 403
        warm-up must also be the one that renders.
        """
        import base64 as _b64
        import os as _os

        self.start()
        self.visit(url, wait=wait)
        dest = str(dest)
        _os.makedirs(_os.path.dirname(dest) or ".", exist_ok=True)

        if self.backend == "playwright":
            if css:
                self._page.add_style_tag(content=css)
            self._page.emulate_media(media="screen")
            self._page.pdf(
                path=dest,
                format="A4",
                print_background=True,
                margin={"top": "14mm", "bottom": "14mm", "left": "12mm", "right": "12mm"},
            )
            return

        if css:
            self._driver.execute_script(
                "var s=document.createElement('style');s.textContent=arguments[0];"
                "document.head.appendChild(s);", css,
            )
        result = self._driver.execute_cdp_cmd(
            "Page.printToPDF",
            {
                "printBackground": True, "preferCSSPageSize": True,
                "paperWidth": 8.27, "paperHeight": 11.69,
                "marginTop": 0.55, "marginBottom": 0.55,
                "marginLeft": 0.47, "marginRight": 0.47,
                "transferMode": "ReturnAsBase64",
            },
        )
        data = _b64.b64decode(result["data"])
        if not data.startswith(b"%PDF-"):
            raise BrowserUnavailable("the browser returned something that is not a PDF")
        with open(dest, "wb") as handle:
            handle.write(data)

    def fetch_bytes(self, url: str, timeout: int = 120) -> bytes:
        """Download a binary URL from inside the page context (same cookies, same TLS)."""
        self.start()
        script = """
        const url = arguments[0];
        const done = arguments[arguments.length - 1];
        fetch(url, {credentials: 'include'})
          .then(r => r.ok ? r.arrayBuffer() : Promise.reject('HTTP ' + r.status))
          .then(b => {
            let s = '';
            const bytes = new Uint8Array(b);
            const step = 0x8000;
            for (let i = 0; i < bytes.length; i += step) {
              s += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
            }
            done({ok: true, data: btoa(s)});
          })
          .catch(e => done({ok: false, error: String(e)}));
        """
        if self.backend == "playwright":
            result = self._page.evaluate(
                """async (url) => {
                    const r = await fetch(url, {credentials: 'include'});
                    if (!r.ok) return {ok: false, error: 'HTTP ' + r.status};
                    const b = new Uint8Array(await r.arrayBuffer());
                    let s = '';
                    const step = 0x8000;
                    for (let i = 0; i < b.length; i += step) {
                        s += String.fromCharCode.apply(null, b.subarray(i, i + step));
                    }
                    return {ok: true, data: btoa(s)};
                }""",
                url,
            )
        else:
            self._driver.set_script_timeout(timeout)
            result = self._driver.execute_async_script(script, url)
        if not result or not result.get("ok"):
            raise BrowserUnavailable((result or {}).get("error", "browser fetch failed"))
        return base64.b64decode(result["data"])
