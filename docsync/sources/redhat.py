"""docs.redhat.com discovery.

Two modes:

* product mode - one product plus one version, e.g.
  red_hat_openshift_ai_self-managed / 3.5
* portal mode  - a curated landing page such as /red_hat_ai/3 that mostly links
  to *other* products. Those products are followed one level deep so a single
  source id yields the whole portfolio.

For every guide we try, in order:
  1. a real PDF link found in the guide page itself,
  2. constructed PDF URLs (Product-Version-Guide-en-US.pdf) verified with a
     redirect-aware probe,
  3. conversion of the html-single page (handled by the downloader).
"""

from __future__ import annotations

import itertools
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urljoin, urlparse

from ..http_client import HttpError
from ..util import (
    LOG,
    clean_segment,
    compile_patterns,
    dedupe,
    looks_like_version,
    matches_any,
    pick_latest,
    prettify_slug,
    safe_filename,
)
from .base import Doc, Source, make_soup

HOST = "https://docs.redhat.com"
DOC_ROOT = f"{HOST}/en/documentation"

GUIDE_RE = re.compile(
    r"/(?:[a-z]{2}(?:-[a-z]{2})?/)?documentation/"
    r"(?P<product>[a-z0-9][a-z0-9_.\-]*)/(?P<version>[^/#?]+)/html(?P<single>-single)?/(?P<slug>[^/#?]+)",
    re.IGNORECASE,
)
VERSION_RE = re.compile(
    r"/(?:[a-z]{2}(?:-[a-z]{2})?/)?documentation/(?P<product>[a-z0-9][a-z0-9_.\-]*)/(?P<version>[^/#?]+)/?$",
    re.IGNORECASE,
)
PRODUCT_ROOT_RE = re.compile(
    r"/(?:[a-z]{2}(?:-[a-z]{2})?/)?documentation/(?P<product>[a-z0-9][a-z0-9_.\-]*)/?$",
    re.IGNORECASE,
)
PDF_HREF_RE = re.compile(r'"(/(?:[a-z]{2}(?:-[a-z]{2})?/)?documentation/[^"\']*?/pdf/[^"\']+?\.pdf)"', re.IGNORECASE)

SKIP_SLUGS = {"index", "legal-notice", "making-open-source-more-inclusive"}
# Not documentation versions, just other pages under /documentation/.
SKIP_VERSION_TOKENS = {"html", "html-single", "pdf", "epub", "index", "topics", "all"}


@dataclass
class ProductInfo:
    slug: str
    display: str
    version: str
    index_url: str
    available_versions: List[str]


def parse_doc_title(title: str) -> Tuple[str, str, Optional[str]]:
    """Split a docs.redhat.com <title> into (guide, product, version).

    Handles both the current format
        "Release notes | Red Hat Connectivity Link | 1.4 | Red Hat Documentation"
    and the older one
        "Release notes | Red Hat OpenShift AI Self-Managed 3.4 | Red Hat ..."
    """
    parts = [p.strip() for p in (title or "").split("|") if p.strip()]
    parts = [p for p in parts if "red hat documentation" not in p.lower()]
    if not parts:
        return "", "", None

    guide = parts[0]
    if len(parts) >= 3 and looks_like_version(parts[2]):
        return guide, parts[1], parts[2]
    if len(parts) >= 2:
        product_version = parts[1]
        head, _, tail = product_version.rpartition(" ")
        if head and looks_like_version(tail):
            return guide, head, tail
        return guide, product_version, None
    return guide, "", None


def parse_index_title(title: str) -> Tuple[str, Optional[str]]:
    """'Red Hat AI | 3 | Red Hat Documentation' -> ('Red Hat AI', '3')."""
    parts = [p.strip() for p in (title or "").split("|") if p.strip()]
    parts = [p for p in parts if "red hat documentation" not in p.lower()]
    if not parts:
        return "", None
    if len(parts) >= 2 and looks_like_version(parts[1]):
        return parts[0], parts[1]
    head, _, tail = parts[0].rpartition(" ")
    if head and looks_like_version(tail):
        return head, tail
    return parts[0], None


def _absolute(href: str) -> str:
    return urljoin(HOST + "/", href)


def _is_docs_host(url: str) -> bool:
    return urlparse(url).netloc.lower() in ("docs.redhat.com", "")


class RedHatDocsSource(Source):
    kind = "redhat"

    def __init__(
        self,
        id: str,
        product: str,
        version: str = "latest",
        label: Optional[str] = None,
        portal: bool = False,
        expand_portal: bool = True,
        include_products: Optional[Sequence[str]] = None,
        exclude_products: Optional[Sequence[str]] = None,
        community: bool = False,
    ) -> None:
        self.id = id
        self.product = product
        self.requested_version = version or "latest"
        self.label = label or prettify_slug(product).replace("_", " ")
        self.portal = portal
        self.expand_portal = expand_portal
        self.include_products = compile_patterns(include_products)
        self.exclude_products = compile_patterns(exclude_products)
        self.community = community

    # -- version handling -------------------------------------------------
    def resolve_product(self, http, product: str, version: str) -> Optional[ProductInfo]:
        """Turn ('red_hat_ai', 'latest') into a concrete product + version."""
        index_url = f"{DOC_ROOT}/{product}/{version}" if version else f"{DOC_ROOT}/{product}"
        try:
            html, final_url = http.get_text(index_url)
        except HttpError as exc:
            LOG.warning("[%s] cannot open %s (%s)", self.id, index_url, exc)
            if version and version != "latest":
                return None
            return self._resolve_from_product_root(http, product)

        soup = make_soup(html)
        display, parsed_version = parse_index_title(soup.title.string if soup.title else "")
        versions = self._versions_on_page(soup, product)

        resolved = parsed_version
        if version and version != "latest" and not resolved:
            resolved = version
        if not resolved:
            resolved = pick_latest(versions) or version or "latest"
        if version and version not in ("latest", "") and resolved != version:
            LOG.debug("[%s] page reports version %s for requested %s", self.id, resolved, version)
            resolved = version

        if not display:
            display = prettify_slug(product).replace("_", " ")
        return ProductInfo(product, display, resolved, final_url, versions)

    def _resolve_from_product_root(self, http, product: str) -> Optional[ProductInfo]:
        root = f"{DOC_ROOT}/{product}"
        try:
            html, final_url = http.get_text(root)
        except HttpError as exc:
            LOG.warning("[%s] product %s is not reachable (%s)", self.id, product, exc)
            return None
        soup = make_soup(html)
        display, _ = parse_index_title(soup.title.string if soup.title else "")
        versions = self._versions_on_page(soup, product)
        latest = pick_latest(versions)
        if not latest:
            LOG.warning("[%s] no versions found on %s", self.id, root)
            return None
        return ProductInfo(product, display or prettify_slug(product).replace("_", " "),
                           latest, f"{DOC_ROOT}/{product}/{latest}", versions)

    @staticmethod
    def _versions_on_page(soup, product: str) -> List[str]:
        found = []
        for anchor in soup.find_all("a", href=True):
            match = VERSION_RE.search(anchor["href"])
            if not match:
                continue
            if match.group("product").lower() != product.lower():
                continue
            version = match.group("version")
            if version.lower() in SKIP_VERSION_TOKENS:
                continue
            found.append(version)
        return dedupe(found)

    # -- discovery --------------------------------------------------------
    def discover(self, http, workers: int = 8) -> List[Doc]:
        info = self.resolve_product(http, self.product, self.requested_version)
        if not info:
            return []
        LOG.info("[%s] %s %s", self.id, info.display, info.version)

        try:
            html, _ = http.get_text(info.index_url)
        except HttpError as exc:
            LOG.error("[%s] index unreadable: %s", self.id, exc)
            return []
        soup = make_soup(html)

        targets: List[Tuple[ProductInfo, List[str]]] = []
        own_slugs = self._guide_slugs(soup, info.slug)
        if own_slugs:
            targets.append((info, own_slugs))

        if self.portal and self.expand_portal:
            for product, version in self._related_products(soup, skip=info.slug):
                if self.include_products and not matches_any(product, self.include_products):
                    continue
                if matches_any(product, self.exclude_products):
                    LOG.debug("[%s] skipping related product %s", self.id, product)
                    continue
                sub = self.resolve_product(http, product, version)
                if not sub:
                    continue
                try:
                    sub_html, _ = http.get_text(sub.index_url)
                except HttpError as exc:
                    LOG.warning("[%s] cannot open %s (%s)", self.id, sub.index_url, exc)
                    continue
                slugs = self._guide_slugs(make_soup(sub_html), sub.slug)
                if slugs:
                    LOG.info("[%s]   + %s %s (%d guides)", self.id, sub.display, sub.version, len(slugs))
                    targets.append((sub, slugs))

        docs: List[Doc] = []
        seen: Set[str] = set()
        for product_info, slugs in targets:
            todo = [s for s in slugs if f"{product_info.slug}/{product_info.version}/{s}" not in seen]
            seen.update(f"{product_info.slug}/{product_info.version}/{s}" for s in todo)
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                for doc in pool.map(lambda s: self._build_doc(http, product_info, s), todo):
                    if doc:
                        docs.append(doc)
        docs.sort(key=lambda d: (d.product, d.version, d.title))
        return docs

    def _guide_slugs(self, soup, product: str) -> List[str]:
        slugs = []
        for anchor in soup.find_all("a", href=True):
            match = GUIDE_RE.search(anchor["href"])
            if not match:
                continue
            if match.group("product").lower() != product.lower():
                continue
            slug = match.group("slug")
            if slug.lower() in SKIP_SLUGS:
                continue
            slugs.append(slug)
        return sorted(dedupe(slugs))

    def _related_products(self, soup, skip: str) -> List[Tuple[str, str]]:
        """Other (product, version) pairs linked from a portal page."""
        pairs: List[Tuple[str, str]] = []
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if not _is_docs_host(_absolute(href)):
                continue
            match = GUIDE_RE.search(href) or VERSION_RE.search(href)
            if match:
                product = match.group("product")
                version = match.group("version") if "version" in match.groupdict() else "latest"
                if version.lower() in SKIP_VERSION_TOKENS:
                    version = "latest"
            else:
                root = PRODUCT_ROOT_RE.search(href)
                if not root:
                    continue
                product, version = root.group("product"), "latest"
            if product.lower() == skip.lower():
                continue
            pairs.append((product.lower(), version))
        # Prefer an explicit version over "latest" for the same product.
        best: Dict[str, str] = {}
        for product, version in pairs:
            if product not in best or (best[product] == "latest" and version != "latest"):
                best[product] = version
        return sorted(best.items())

    # -- per-guide resolution ---------------------------------------------
    def _build_doc(self, http, info: ProductInfo, slug: str) -> Optional[Doc]:
        page_url = f"{DOC_ROOT}/{info.slug}/{info.version}/html/{slug}/index"
        single_url = f"{DOC_ROOT}/{info.slug}/{info.version}/html-single/{slug}/index"
        title = prettify_slug(slug).replace("_", " ")
        product_display = info.display
        direct_pdf: Optional[str] = None
        alt_titles: List[str] = []

        try:
            html, final_url = http.get_text(page_url)
            soup = make_soup(html)
            raw_title = soup.title.string if soup.title else ""
            guide, product, version = parse_doc_title(raw_title)
            if guide:
                title = guide
            if product:
                product_display = product
            if version and looks_like_version(version):
                info = ProductInfo(info.slug, product_display, version, info.index_url, info.available_versions)
            heading = soup.find("h1")
            if heading and heading.get_text(strip=True):
                alt_titles.append(heading.get_text(strip=True))
            direct_pdf = self._find_pdf_link(soup, html, slug, final_url)
        except HttpError as exc:
            LOG.debug("[%s] %s: guide page unavailable (%s)", self.id, slug, exc)

        candidates = self._pdf_candidates(info, slug, [title] + alt_titles, product_display)
        if direct_pdf:
            candidates.insert(0, direct_pdf)

        filename = safe_filename(candidates[0].rsplit("/", 1)[-1]) if candidates else \
            safe_filename(f"{clean_segment(product_display)}-{info.version}-{clean_segment(title)}-en-US.pdf")

        return Doc(
            key=f"redhat:{info.slug}:{info.version}:{slug}",
            source_id=self.id,
            product=product_display,
            version=info.version,
            title=title,
            filename=filename,
            rel_dir=f"{info.slug}/{info.version}",
            page_url=page_url,
            pdf_url=candidates[0] if candidates else None,
            pdf_candidates=candidates,
            render_urls=[single_url],
            render_titles=[title],
        )

    @staticmethod
    def _find_pdf_link(soup, html: str, slug: str, base_url: str) -> Optional[str]:
        """A PDF link published by the page itself always beats a guess."""
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if ".pdf" in href.lower() and f"/pdf/{slug}/" in href.lower():
                return urljoin(base_url, href)
        for match in PDF_HREF_RE.finditer(html):
            href = match.group(1)
            if f"/pdf/{slug}/" in href.lower():
                return urljoin(base_url, href)
        return None

    def _pdf_candidates(self, info: ProductInfo, slug: str, titles: Sequence[str],
                        product_display: str) -> List[str]:
        products = dedupe([product_display, info.display, prettify_slug(info.slug).replace("_", " ")])
        guides = dedupe([t for t in titles if t] + [prettify_slug(slug).replace("_", " ")])
        base = f"{DOC_ROOT}/{info.slug}/{info.version}/pdf/{slug}"

        urls: List[str] = []
        for product, guide in itertools.product(products[:2], guides[:3]):
            name = f"{clean_segment(product)}-{clean_segment(info.version)}-{clean_segment(guide)}-en-US.pdf"
            urls.append(f"{base}/{name}")
        # Older content on the portal uses an all-lowercase filename.
        if urls:
            urls.append(f"{base}/{urls[0].rsplit('/', 1)[-1].lower()}")
        return dedupe(urls)[:6]
