"""Offline checks - no network. Run with: python tests/test_offline.py"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docsync.catalog import build_catalog, select_sources
from docsync.cli import build_parser, parse_version_overrides
from docsync.manifest import Entry, Manifest
from docsync.sources.mkdocs_site import MkDocsSource
from docsync.sources.redhat import RedHatDocsSource, parse_doc_title, parse_index_title
from docsync.util import clean_segment, pick_latest, prettify_slug, version_key

PRODUCT_INDEX = """
<html><head><title>Red Hat OpenShift AI Self-Managed | 3.4 | Red Hat Documentation</title></head>
<body>
<a href="/en/documentation/red_hat_openshift_ai_self-managed/3.3/">3.3</a>
<a href="/en/documentation/red_hat_openshift_ai_self-managed/3.4/">3.4</a>
<a href="/en/documentation/red_hat_openshift_ai_self-managed/3.4/html/release_notes">Release notes</a>
<a href="/en/documentation/red_hat_openshift_ai_self-managed/3.4/html-single/deploying_models/index#x">Deploy</a>
<a href="/en/documentation/red_hat_ai_inference_server/latest/html/getting_started/index">Other product</a>
<a href="https://access.redhat.com/articles/7133758">KB article</a>
</body></html>
"""

def guide_page(title, product="Red Hat OpenShift AI Self-Managed", version="3.4", extra=""):
    return (f"<html><head><title>{title} | {product} | {version} | Red Hat Documentation</title></head>"
            f"<body><h1>{title}</h1>{extra}</body></html>")


GUIDE_PAGE = guide_page("Release notes")

PORTAL_INDEX = """
<html><head><title>Red Hat AI | 3 | Red Hat Documentation</title></head>
<body>
<a href="https://docs.redhat.com/en/documentation/red_hat_ai/3/html/validated_models">Validated models</a>
<a href="https://docs.redhat.com/en/documentation/red_hat_openshift_ai_self-managed/latest/html/release_notes/">RN</a>
<a href="https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html/x/">RHEL</a>
</body></html>
"""


class FakeHttp:
    """Minimal stand-in for HttpClient: serves canned pages."""

    def __init__(self, pages):
        self.pages = pages
        self.seen = []

    def get_text(self, url, **kwargs):
        self.seen.append(url)
        key = url.rstrip("/")
        for suffix, body in self.pages.items():
            if key.endswith(suffix.rstrip("/")):
                return body, url
        from docsync.http_client import HttpError
        raise HttpError(f"404 {url}")

    def fingerprint(self, url):
        return "etag-" + url[-6:]


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + detail) if detail and not condition else ''}")
    return bool(condition)


def main():
    ok = True

    # --- filename hygiene -------------------------------------------------
    ok &= check("clean_segment keeps hyphens, folds spaces",
                clean_segment("Red Hat OpenShift AI Self-Managed") == "Red_Hat_OpenShift_AI_Self-Managed",
                clean_segment("Red Hat OpenShift AI Self-Managed"))
    ok &= check("clean_segment drops trademark glyphs",
                clean_segment("Red Hat\u00ae AI") == "Red_Hat_AI", clean_segment("Red Hat\u00ae AI"))
    ok &= check("prettify_slug capitalises known tokens",
                prettify_slug("red_hat_openshift_ai_self-managed") == "Red_Hat_OpenShift_AI_Self-Managed",
                prettify_slug("red_hat_openshift_ai_self-managed"))

    # --- title parsing ----------------------------------------------------
    ok &= check("current 4-part title",
                parse_doc_title("Release notes | Red Hat Connectivity Link | 1.4 | Red Hat Documentation")
                == ("Release notes", "Red Hat Connectivity Link", "1.4"))
    ok &= check("legacy 3-part title",
                parse_doc_title("Monitoring your AI systems | Red Hat OpenShift AI Self-Managed 3.4 | Red Hat Documentation")
                == ("Monitoring your AI systems", "Red Hat OpenShift AI Self-Managed", "3.4"))
    ok &= check("index title", parse_index_title("Red Hat AI | 3 | Red Hat Documentation") == ("Red Hat AI", "3"))

    # --- version ordering -------------------------------------------------
    ok &= check("version sort", pick_latest(["2.25", "3.4", "3.10", "1.0"]) == "3.10",
                pick_latest(["2.25", "3.4", "3.10", "1.0"]))
    ok &= check("mkdocs-style version sort", pick_latest(["1.4.x", "1.5.x", "dev"]) == "1.5.x",
                pick_latest(["1.4.x", "1.5.x", "dev"]))
    ok &= check("version_key is total", version_key("3.4") < version_key("3.10"))

    # --- Red Hat discovery ------------------------------------------------
    http = FakeHttp({
        "red_hat_openshift_ai_self-managed/3.4/html/release_notes/index": GUIDE_PAGE,
        "red_hat_openshift_ai_self-managed/3.4/html/deploying_models/index": guide_page("Deploying models"),
        "red_hat_openshift_ai_self-managed/3.4": PRODUCT_INDEX,
    })
    source = RedHatDocsSource(id="rhoai", product="red_hat_openshift_ai_self-managed", version="3.4")
    docs = source.discover(http, workers=2)
    titles = sorted(d.title for d in docs)
    ok &= check("two guides discovered, other products ignored", len(docs) == 2, str(titles))

    release = next((d for d in docs if "release" in d.title.lower()), None)
    expected = ("https://docs.redhat.com/en/documentation/red_hat_openshift_ai_self-managed/3.4/"
                "pdf/release_notes/Red_Hat_OpenShift_AI_Self-Managed-3.4-Release_notes-en-US.pdf")
    ok &= check("constructed PDF URL matches the real one",
                release is not None and release.pdf_candidates[0] == expected,
                release.pdf_candidates[0] if release else "no doc")
    ok &= check("html-single URL kept for conversion fallback",
                release is not None and release.render_urls[0].endswith("/html-single/release_notes/index"))
    ok &= check("stable key", release is not None and release.key ==
                "redhat:red_hat_openshift_ai_self-managed:3.4:release_notes")

    # --- portal expansion -------------------------------------------------
    portal_http = FakeHttp({
        "red_hat_ai/3/html/validated_models/index": guide_page("Validated models", "Red Hat AI", "3"),
        "red_hat_openshift_ai_self-managed/3.4/html/release_notes/index": GUIDE_PAGE,
        "red_hat_openshift_ai_self-managed/latest": PRODUCT_INDEX,
        "red_hat_ai/3": PORTAL_INDEX,
    })
    portal = RedHatDocsSource(id="rhai", product="red_hat_ai", version="3", portal=True,
                              exclude_products=[r"^red_hat_enterprise_linux$"])
    portal_docs = portal.discover(portal_http, workers=2)
    followed = [u for u in portal_http.seen if "red_hat_openshift_ai_self-managed" in u]
    excluded = [u for u in portal_http.seen if "/red_hat_enterprise_linux/" in u]
    ok &= check("portal follows linked products", bool(followed), str(portal_http.seen))
    ok &= check("portal honours product exclusions", not excluded, str(excluded))
    ok &= check("portal still returns its own guides", any("Validated" in d.title or "validated" in d.title.lower()
                                                           for d in portal_docs), str([d.title for d in portal_docs]))

    # --- mkdocs helpers ---------------------------------------------------
    site = MkDocsSource(id="kuadrant", label="Kuadrant", root_url="https://docs.kuadrant.io", version="1.5.x")
    base = site.base_url("1.5.x")
    ok &= check("mkdocs base url", base == "https://docs.kuadrant.io/1.5.x", base)
    ordered = site._sort_pages(
        [f"{base}/z/", f"{base}/getting-started/", f"{base}/"],
        [f"{base}/", f"{base}/getting-started/"],
    )
    ok &= check("nav order wins over sitemap order",
                ordered == [f"{base}/", f"{base}/getting-started/", f"{base}/z/"], str(ordered))
    ok &= check("page filtering", not MkDocsSource(id="x", label="x", root_url="http://e",
                                                   exclude=["/blog/"])._wanted("http://e/1/blog/post/"))

    # --- manifest ---------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        (out / "a.pdf").write_bytes(b"%PDF-1.7 test")
        manifest = Manifest(out)
        manifest.put(Entry(key="k1", path="a.pdf", url="http://x/a.pdf", method="pdf", size=13))
        manifest.put(Entry(key="k2", path="gone.pdf", url="http://x/gone.pdf", method="pdf"))
        manifest.save()
        reloaded = Manifest(out)
        ok &= check("manifest round-trips", reloaded.get("k1") is not None and reloaded.get("k1").size == 13)
        ok &= check("known() requires the file on disk",
                    reloaded.known("k1") and not reloaded.known("k2"))
        ok &= check("prune drops missing files", reloaded.forget_missing() == 1)

    # --- CLI wiring -------------------------------------------------------
    parser = build_parser()
    args = parser.parse_args(["download", "--source", "rhoai,rhcl", "--version", "rhoai=3.5",
                              "--out", "/tmp/x", "--only-new"])
    ok &= check("CLI parses comma-separated sources", args.source == ["rhoai,rhcl"] and args.only_new)
    overrides = parse_version_overrides(args.version)
    ok &= check("per-source version override", overrides == {"rhoai": "3.5"}, str(overrides))
    catalog = build_catalog(overrides)
    ok &= check("override reaches the catalog", catalog["rhoai"].requested_version == "3.5")
    selected = select_sources(catalog, args.source)
    ok &= check("source selection", [s.id for s in selected] == ["rhoai", "rhcl"],
                str([s.id for s in selected]))
    ok &= check("'community' keyword selects community sources",
                {s.id for s in select_sources(catalog, ["community"])} == {"maas", "kuadrant"})
    global_override = build_catalog(parse_version_overrides(["3.4"]))
    ok &= check("bare --version applies to every source",
                global_override["rhoai"].requested_version == "3.4")

    print("\nALL PASSED" if ok else "\nSOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
