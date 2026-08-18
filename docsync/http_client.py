"""HTTP layer: retries, throttling, redirect-aware PDF probing, atomic downloads.

Two things beyond plain `requests` matter here:

* docs.redhat.com answers a wrong PDF filename with a redirect to a landing page
  that returns 200 text/html, so a status check alone is not enough;
* the site sits behind a bot-protection layer that can answer a scripted request
  with 403 even when the URL opens fine in a browser. When that happens we can
  borrow cookies from a real headless browser and retry.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

try:  # urllib3 v2 and v1 keep Retry in the same place, but be defensive.
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

from .util import LOG

PDF_MAGIC = b"%PDF-"
DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
RETRY_STATUSES = (408, 425, 429, 500, 502, 503, 504)
BLOCKED_STATUSES = (401, 403, 406, 429)

# A scripted request is recognisable mostly by what it does *not* send. These are
# the headers a real Chrome sends on a top-level navigation.
BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
}
PDF_ACCEPT = "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8"


@dataclass
class ProbeResult:
    """Outcome of asking 'is there really a PDF at this URL?'."""

    ok: bool
    status: int = 0
    url: str = ""
    final_url: str = ""
    content_type: str = ""
    length: Optional[int] = None
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    reason: str = ""


@dataclass
class DownloadResult:
    status: str  # downloaded | unchanged | failed
    path: Optional[Path] = None
    size: Optional[int] = None
    sha256: Optional[str] = None
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    final_url: str = ""
    error: str = ""
    headers: Dict[str, str] = field(default_factory=dict)


@dataclass
class Diagnosis:
    url: str
    status: int = 0
    ok: bool = False
    server: str = ""
    verdict: str = ""
    detail: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    body_snippet: str = ""


class HttpError(Exception):
    def __init__(self, message: str, status: int = 0, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url


class HttpClient:
    """A thin, well-behaved wrapper around requests.Session."""

    def __init__(
        self,
        timeout: float = 30.0,
        connect_timeout: float = 10.0,
        retries: int = 3,
        backoff: float = 0.6,
        rate_limit: float = 0.0,
        user_agent: str = DEFAULT_UA,
        verify_tls: bool = True,
        proxy: Optional[str] = None,
        browser_fallback: str = "auto",   # auto | on | off
        browser_engine: str = "auto",
    ) -> None:
        self.timeout = (connect_timeout, timeout)
        self.rate_limit = max(rate_limit, 0.0)
        self._last_request = 0.0
        self._lock = threading.Lock()

        self.browser_fallback = browser_fallback
        self.browser_engine = browser_engine
        self._browser = None
        self._browser_failed = False
        self._warmed: set = set()
        self._browser_lock = threading.Lock()

        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)
        self.session.headers["User-Agent"] = user_agent
        self.session.verify = verify_tls
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

        retry = Retry(
            total=retries,
            connect=retries,
            read=retries,
            status=retries,
            backoff_factor=backoff,
            status_forcelist=RETRY_STATUSES,
            allowed_methods=frozenset({"GET", "HEAD"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # -- plumbing ---------------------------------------------------------
    def _throttle(self) -> None:
        if not self.rate_limit:
            return
        with self._lock:
            wait = self.rate_limit - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def request(self, method: str, url: str, referer: Optional[str] = None, **kwargs):
        self._throttle()
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("allow_redirects", True)
        if referer:
            headers = dict(kwargs.pop("headers", None) or {})
            headers.setdefault("Referer", referer)
            headers.setdefault("Sec-Fetch-Site", "same-origin")
            kwargs["headers"] = headers
        LOG.debug("%s %s", method, url)
        return self.session.request(method, url, **kwargs)

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def head(self, url: str, **kwargs):
        return self.request("HEAD", url, **kwargs)

    # -- browser escalation -----------------------------------------------
    def _browser_enabled(self) -> bool:
        return self.browser_fallback in ("auto", "on") and not self._browser_failed

    def _get_browser(self):
        if not self._browser_enabled():
            return None
        if self._browser is not None:
            return self._browser
        from .browser import BrowserSession, BrowserUnavailable

        session = BrowserSession(engine=self.browser_engine)
        if not session.available():
            LOG.debug("no browser installed for the 403 fallback")
            self._browser_failed = True
            return None
        try:
            session.start()
        except BrowserUnavailable as exc:
            LOG.warning("browser fallback unavailable: %s", exc)
            self._browser_failed = True
            return None
        self._browser = session
        return session

    def warm_up(self, url: str, force: bool = False) -> bool:
        """Open `url` in a browser and copy its cookies into the session.

        Returns True when cookies were transferred, i.e. it is worth retrying.
        """
        host = urlparse(url).netloc.lower()
        with self._browser_lock:
            if host in self._warmed and not force:
                return False
            browser = self._get_browser()
            if browser is None:
                return False
            try:
                LOG.info("request was refused; warming up %s in a headless browser", host)
                browser.visit(f"{urlparse(url).scheme}://{host}/", wait=3.0)
                browser.visit(url, wait=3.0)
                cookies = browser.cookies()
                agent = browser.user_agent()
            except Exception as exc:
                LOG.warning("browser warm-up failed: %s", exc)
                self._browser_failed = True
                self._warmed.add(host)
                return False
            for name, value in cookies.items():
                self.session.cookies.set(name, value, domain=host)
            if agent:
                self.session.headers["User-Agent"] = agent
            self._warmed.add(host)
            LOG.debug("carried %d cookie(s) over from the browser", len(cookies))
            return bool(cookies)

    def active_browser(self):
        """The browser already running for the 403 fallback, if any.

        Never starts one - callers use this to reuse an open session rather
        than launching a second, conflicting instance.
        """
        return self._browser

    def browser_html(self, url: str) -> Optional[str]:
        browser = self._get_browser()
        if browser is None:
            return None
        try:
            return browser.get_html(url, wait=2.5)
        except Exception as exc:
            LOG.debug("browser could not load %s: %s", url, exc)
            return None

    def browser_bytes(self, url: str, referer: Optional[str] = None) -> Optional[bytes]:
        browser = self._get_browser()
        if browser is None:
            return None
        try:
            if referer:
                browser.visit(referer, wait=1.0)
            return browser.fetch_bytes(url)
        except Exception as exc:
            LOG.debug("browser could not fetch %s: %s", url, exc)
            return None

    # -- text fetching -----------------------------------------------------
    def get_text(self, url: str, referer: Optional[str] = None, _retry: bool = True, **kwargs) -> Tuple[str, str]:
        """Return (text, final_url). Raises HttpError on a non-2xx response."""
        try:
            resp = self.get(url, referer=referer, **kwargs)
        except requests.RequestException as exc:
            raise HttpError(f"{type(exc).__name__}: {exc}", url=url) from exc

        if resp.status_code in BLOCKED_STATUSES and _retry and self._browser_enabled():
            if self.warm_up(url):
                return self.get_text(url, referer=referer, _retry=False, **kwargs)
            html = self.browser_html(url)
            if html:
                LOG.debug("served %s from the browser instead", url)
                return html, url

        if resp.status_code >= 400:
            hint = ""
            if resp.status_code in BLOCKED_STATUSES:
                hint = " - the request was refused, not missing; run 'docsync doctor' for details"
            raise HttpError(f"HTTP {resp.status_code} for {url}{hint}", status=resp.status_code, url=url)
        if not resp.encoding:
            resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text, resp.url

    # -- PDF probing ------------------------------------------------------
    def probe_pdf(self, url: str, referer: Optional[str] = None, _retry: bool = True) -> ProbeResult:
        """Check whether `url` really serves a PDF."""
        try:
            resp = self.head(url, referer=referer, headers={"Accept": PDF_ACCEPT})
        except requests.RequestException as exc:
            return ProbeResult(False, url=url, reason=f"{type(exc).__name__}: {exc}")

        status = resp.status_code
        ctype = (resp.headers.get("Content-Type") or "").lower()
        final = resp.url
        redirected = final.split("#")[0].rstrip("/") != url.split("#")[0].rstrip("/")

        if status == 200 and "pdf" in ctype:
            return ProbeResult(
                True,
                status=status,
                url=url,
                final_url=final,
                content_type=ctype,
                length=_int_or_none(resp.headers.get("Content-Length")),
                etag=resp.headers.get("ETag"),
                last_modified=resp.headers.get("Last-Modified"),
            )

        if status in BLOCKED_STATUSES and _retry and self._browser_enabled():
            if self.warm_up(url):
                return self.probe_pdf(url, referer=referer, _retry=False)

        if status == 200 and ("html" in ctype or (redirected and not final.lower().endswith(".pdf"))):
            return ProbeResult(
                False,
                status=status,
                url=url,
                final_url=final,
                content_type=ctype,
                reason=f"redirected to a non-PDF page ({final})" if redirected else "server returned HTML",
            )

        # Ambiguous (empty/octet-stream content type) or HEAD not allowed: look at bytes.
        if status == 200 or status in (403, 405, 501):
            return self._probe_by_bytes(url, referer=referer)

        return ProbeResult(False, status=status, url=url, final_url=final, reason=f"HTTP {status}")

    def _probe_by_bytes(self, url: str, referer: Optional[str] = None) -> ProbeResult:
        try:
            with self.get(url, stream=True, referer=referer,
                          headers={"Range": "bytes=0-2047", "Accept": PDF_ACCEPT}) as resp:
                if resp.status_code >= 400:
                    return ProbeResult(False, status=resp.status_code, url=url, final_url=resp.url,
                                       reason=f"HTTP {resp.status_code}")
                head = next(resp.iter_content(chunk_size=2048), b"") or b""
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if head.startswith(PDF_MAGIC):
                    return ProbeResult(
                        True,
                        status=resp.status_code,
                        url=url,
                        final_url=resp.url,
                        content_type=ctype or "application/pdf",
                        length=_int_or_none(resp.headers.get("Content-Length")),
                        etag=resp.headers.get("ETag"),
                        last_modified=resp.headers.get("Last-Modified"),
                    )
                return ProbeResult(False, status=resp.status_code, url=url, final_url=resp.url,
                                   content_type=ctype, reason="response is not a PDF")
        except requests.RequestException as exc:
            return ProbeResult(False, url=url, reason=f"{type(exc).__name__}: {exc}")

    # -- downloading ------------------------------------------------------
    def download(
        self,
        url: str,
        dest: Path,
        expect_pdf: bool = True,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
        referer: Optional[str] = None,
    ) -> DownloadResult:
        """Stream `url` to `dest` atomically. Honours ETag / Last-Modified."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        headers: Dict[str, str] = {"Accept": PDF_ACCEPT if expect_pdf else "*/*"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        tmp = dest.with_name(dest.name + ".part")
        digest = hashlib.sha256()
        written = 0
        try:
            with self.get(url, stream=True, headers=headers, referer=referer) as resp:
                if resp.status_code == 304:
                    return DownloadResult("unchanged", path=dest, final_url=resp.url)

                if resp.status_code in BLOCKED_STATUSES:
                    if self.warm_up(url):
                        return self.download(url, dest, expect_pdf, etag, last_modified, referer)
                    data = self.browser_bytes(url, referer=referer) if expect_pdf else None
                    if data and data.startswith(PDF_MAGIC):
                        dest.write_bytes(data)
                        return DownloadResult(
                            "downloaded", path=dest, size=len(data),
                            sha256=hashlib.sha256(data).hexdigest(), final_url=url,
                        )
                    return DownloadResult("failed", error=f"HTTP {resp.status_code} (request refused)",
                                          final_url=resp.url)

                if resp.status_code >= 400:
                    return DownloadResult("failed", error=f"HTTP {resp.status_code}", final_url=resp.url)

                ctype = (resp.headers.get("Content-Type") or "").lower()
                if expect_pdf and "html" in ctype:
                    return DownloadResult(
                        "failed",
                        error=f"server returned HTML instead of a PDF ({resp.url})",
                        final_url=resp.url,
                    )

                first = True
                with open(tmp, "wb") as handle:
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        if first:
                            first = False
                            if expect_pdf and not chunk.startswith(PDF_MAGIC):
                                handle.close()
                                tmp.unlink(missing_ok=True)
                                return DownloadResult(
                                    "failed",
                                    error="payload is not a PDF (bad magic bytes)",
                                    final_url=resp.url,
                                )
                        handle.write(chunk)
                        digest.update(chunk)
                        written += len(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())

                if written == 0:
                    tmp.unlink(missing_ok=True)
                    return DownloadResult("failed", error="empty response body", final_url=resp.url)

                os.replace(tmp, dest)
                return DownloadResult(
                    "downloaded",
                    path=dest,
                    size=written,
                    sha256=digest.hexdigest(),
                    etag=resp.headers.get("ETag"),
                    last_modified=resp.headers.get("Last-Modified"),
                    final_url=resp.url,
                )
        except requests.RequestException as exc:
            tmp.unlink(missing_ok=True)
            return DownloadResult("failed", error=f"{type(exc).__name__}: {exc}")
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            return DownloadResult("failed", error=f"write error: {exc}")

    def fingerprint(self, url: str) -> str:
        """Cheap change signal for a page: ETag, else Last-Modified, else ''."""
        try:
            resp = self.head(url)
            if resp.status_code >= 400:
                return f"status:{resp.status_code}"
            return resp.headers.get("ETag") or resp.headers.get("Last-Modified") or ""
        except requests.RequestException:
            return ""

    # -- diagnostics -------------------------------------------------------
    def diagnose(self, url: str) -> Diagnosis:
        """Work out *why* a URL is refused: proxy, bot protection, or genuinely gone."""
        try:
            resp = self.get(url, allow_redirects=True)
        except requests.RequestException as exc:
            return Diagnosis(url=url, verdict="connection failed", detail=f"{type(exc).__name__}: {exc}")

        headers = {k.lower(): v for k, v in resp.headers.items()}
        body = (resp.text or "")[:400].replace("\n", " ") if "text" in headers.get("content-type", "") else ""
        result = Diagnosis(
            url=url,
            status=resp.status_code,
            ok=resp.status_code < 400,
            server=headers.get("server", ""),
            headers=headers,
            body_snippet=body.strip(),
        )

        if result.ok:
            result.verdict = "reachable"
            result.detail = f"{resp.status_code} {headers.get('content-type', '')}"
            return result

        deny = headers.get("x-deny-reason") or headers.get("x-squid-error") or headers.get("x-blocked-by")
        proxy_markers = any(k in headers for k in ("proxy-connection", "x-cache-lookup", "x-bluecoat-via", "x-zscaler"))
        akamai = "akamai" in headers.get("server", "").lower() or "reference #" in body.lower() \
            or "akamai" in body.lower()

        if deny or proxy_markers:
            result.verdict = "blocked by a proxy between you and the site"
            result.detail = deny or "proxy headers present in the response"
        elif resp.status_code in (401, 403) and akamai:
            result.verdict = "blocked by the site's bot protection"
            result.detail = "the CDN refused a scripted request; --browser-fallback on usually clears it"
        elif resp.status_code in (401, 403):
            result.verdict = "refused by the site (403)"
            result.detail = "could be bot protection or a network policy; compare with a browser on the same host"
        elif resp.status_code == 404:
            result.verdict = "not found"
            result.detail = "the product slug or version is probably wrong"
        else:
            result.verdict = f"HTTP {resp.status_code}"
            result.detail = headers.get("content-type", "")
        return result

    def close(self) -> None:
        self.session.close()
        if self._browser is not None:
            try:
                self._browser.stop()
            except Exception:
                pass
            self._browser = None


def _int_or_none(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
