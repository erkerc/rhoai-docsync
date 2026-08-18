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
    version_key,
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

#: Above this many guides, hint that the user probably wants to narrow the run.
LARGE_PRODUCT_WARNING = 40

SKIP_SLUGS = {"index", "legal-notice", "making-open-source-more-inclusive"}
# Headings that are page furniture rather than a documentation category.
SKIP_HEADINGS_RE = re.compile(
    r"^(left navigation|jump to category|version|on this page|table of contents|"
    r"featured links|select your language|theme|learn|communities|about red hat)",
    re.IGNORECASE,
)
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
        group_by_category: bool = False,
        categories: Optional[Sequence[str]] = None,
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
        self.group_by_category = group_by_category
        self.categories = compile_patterns(categories)

    # -- version handling -------------------------------------------------
    def resolve_product(self, http, product: str, version: str) -> Optional[ProductInfo]:
        """Turn ('red_hat_ai', 'latest') into a concrete product + version."""
        index_url = f"{DOC_ROOT}/{product}/{version}" if version else f"{DOC_ROOT}/{product}"
        try:
            html, final_url = http.get_text(index_url)
        except HttpError as exc:
            # Not every product serves a /latest alias (OpenShift Container
            # Platform does not); the product root redirects to the newest one.
            LOG.debug("[%s] %s unavailable (%s); trying the product root", self.id, index_url, exc)
            if version and version not in ("latest", "", None):
                LOG.warning("[%s] cannot open %s (%s)", self.id, index_url, exc)
                return None
            return self._resolve_from_product_root(http, product)

        soup = make_soup(html)
        display, parsed_version = parse_index_title(soup.title.string if soup.title else "")
        versions = self._versions_on_page(soup, product)

        if not versions:
            versions = self._versions_from_text(soup)

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
        """Resolve via /documentation/<product>, which redirects to the current version."""
        root = f"{DOC_ROOT}/{product}"
        try:
            html, final_url = http.get_text(root)
        except HttpError as exc:
            LOG.warning("[%s] product %s is not reachable (%s)", self.id, product, exc)
            return None
        soup = make_soup(html)
        display, title_version = parse_index_title(soup.title.string if soup.title else "")
        versions = self._versions_on_page(soup, product)

        # Preference order: the version in the page title (the root redirects to
        # the newest one), then the URL we landed on, then the version links.
        latest = title_version
        if not latest:
            landed = VERSION_RE.search(urlparse(final_url).path)
            if landed and landed.group("version").lower() not in SKIP_VERSION_TOKENS:
                latest = landed.group("version")
        if not latest:
            latest = pick_latest(versions)
        if not latest:
            LOG.warning("[%s] no versions found on %s", self.id, root)
            return None

        if not versions:
            versions = self._versions_from_text(soup)
        return ProductInfo(product, display or prettify_slug(product).replace("_", " "),
                           latest, f"{DOC_ROOT}/{product}/{latest}", dedupe([latest] + versions))

    @staticmethod
    def _versions_from_text(soup) -> List[str]:
        """Some products render the version switcher as plain text, not links."""
        found: List[str] = []
        for element in soup.find_all(["li", "option", "span", "a"]):
            text = element.get_text(" ", strip=True)
            if text and len(text) <= 8 and looks_like_version(text):
                found.append(text)
        return sorted(dedupe(found), key=version_key, reverse=True)[:40]

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

        targets: List[Tuple[ProductInfo, Dict[str, str]]] = []
        own = self._select_categories(self._guide_categories(soup, info.slug))
        if own:
            targets.append((info, own))

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
                slugs = self._select_categories(self._guide_categories(make_soup(sub_html), sub.slug))
                if slugs:
                    LOG.info("[%s]   + %s %s (%d guides)", self.id, sub.display, sub.version, len(slugs))
                    targets.append((sub, slugs))

        total = sum(len(slugs) for _, slugs in targets)
        if total >= LARGE_PRODUCT_WARNING:
            LOG.info(
                "[%s] %d guides - this is a big set; --category, --include or --only-new "
                "will narrow it down",
                self.id, total,
            )

        docs: List[Doc] = []
        seen: Set[str] = set()
        for product_info, slugs in targets:
            todo = [s for s in slugs if f"{product_info.slug}/{product_info.version}/{s}" not in seen]
            seen.update(f"{product_info.slug}/{product_info.version}/{s}" for s in todo)
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                built = pool.map(lambda s: self._build_doc(http, product_info, s, slugs.get(s, "")), todo)
                for doc in built:
                    if doc:
                        docs.append(doc)
        docs.sort(key=lambda d: (d.product, d.version, d.category, d.title))
        return docs

    def _select_categories(self, slugs: Dict[str, str]) -> Dict[str, str]:
        if not self.categories:
            return slugs
        return {slug: cat for slug, cat in slugs.items() if matches_any(cat or slug, self.categories)}

    def _guide_slugs(self, soup, product: str) -> List[str]:
        return sorted(self._guide_categories(soup, product))

    def _guide_categories(self, soup, product: str) -> Dict[str, str]:
        """slug -> the index section it sits under ('Networking', 'Install', ...).

        Large products group their guides under h2 headings; walking the
        document in order lets us keep that structure instead of dumping a
        hundred PDFs into one directory. Products without headings simply get
        an empty category.
        """
        found: Dict[str, str] = {}
        current = ""
        for element in soup.find_all(["h1", "h2", "h3", "a"]):
            name = element.name.lower()
            if name in ("h1", "h2"):
                text = element.get_text(" ", strip=True)
                if text and not SKIP_HEADINGS_RE.search(text):
                    current = re.sub(r"\s+", " ", text)[:60]
                continue
            href = element.get("href")
            if not href:
                continue
            match = GUIDE_RE.search(href)
            if not match or match.group("product").lower() != product.lower():
                continue
            slug = match.group("slug")
            if slug.lower() in SKIP_SLUGS:
                continue
            found.setdefault(slug, current)
        return found

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
    def _build_doc(self, http, info: ProductInfo, slug: str, category: str = "") -> Optional[Doc]:
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

        rel_dir = f"{info.slug}/{info.version}"
        if self.group_by_category and category:
            rel_dir = f"{rel_dir}/{safe_filename(category).replace('/', '-')}"

        return Doc(
            key=f"redhat:{info.slug}:{info.version}:{slug}",
            source_id=self.id,
            product=product_display,
            version=info.version,
            title=title,
            filename=filename,
            rel_dir=rel_dir,
            category=category,
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
