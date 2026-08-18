"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from . import __version__
from .catalog import build_catalog, load_custom_sources, select_sources
from .downloader import DOWNLOADED, CONVERTED, FAILED, Downloader, Options, summarise
from .http_client import HttpClient
from .manifest import Manifest
from .sources.base import Doc
from .sources.mkdocs_site import MkDocsSource
from .sources.redhat import RedHatDocsSource
from .util import LOG, compile_patterns, human_size, matches_any, setup_logging

EPILOG = """\
examples:
  docsync sources
  docsync list --source rhoai
  docsync download --out ~/redhat-docs
  docsync download --source rhoai --version 3.5 --out ~/redhat-docs
  docsync download --community --only-new --out ~/redhat-docs
  docsync download --source ocp --version 4.22 --out ~/redhat-docs
  docsync download --source ocp --category 'network|install' --out ~/redhat-docs
  docsync download --source kuadrant --community-mode per-page --out ~/redhat-docs
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docsync",
        description="Discover and download Red Hat AI, Connectivity Link and community documentation as PDFs.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version-info", action="version", version=f"docsync {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-s", "--source", action="append", metavar="ID",
                        help="source id, or all/redhat/community/default (repeatable, comma-separated)")
    common.add_argument("--version", action="append", metavar="[ID=]VER", default=[],
                        help="version to fetch: '3.5' for every source, or 'rhoai=3.5' for one")
    common.add_argument("--community", action="store_true", help="include the community sources")
    common.add_argument("--config", type=Path, metavar="FILE", help="JSON file with extra source definitions")
    common.add_argument("--include", action="append", metavar="RE", help="only documents matching this regex")
    common.add_argument("--exclude", action="append", metavar="RE", help="drop documents matching this regex")
    common.add_argument("--include-product", action="append", metavar="RE",
                        help="when expanding a portal page, only follow these products")
    common.add_argument("--exclude-product", action="append", metavar="RE",
                        help="when expanding a portal page, skip these products")
    common.add_argument("--category", action="append", metavar="RE",
                        help="only guides in matching index sections, e.g. --category 'network|install' "
                             "(large products such as OpenShift group their guides under headings)")
    common.add_argument("--group-by-category", dest="group_by_category", action="store_true", default=None,
                        help="file guides into per-category sub-directories (default for OpenShift)")
    common.add_argument("--no-group-by-category", dest="group_by_category", action="store_false",
                        help="keep every guide of a product in one directory")
    common.add_argument("--no-portal-expand", action="store_true",
                        help="do not follow products linked from a portal index")
    common.add_argument("--community-mode", choices=("single", "per-page"), default="single",
                        help="one merged PDF per community site (default) or one PDF per page")
    common.add_argument("--no-fingerprint", action="store_true",
                        help="skip the per-page change check for community sites (faster discovery)")
    common.add_argument("-w", "--workers", type=int, default=6, help="parallel requests (default 6)")
    common.add_argument("--timeout", type=float, default=30.0, help="read timeout in seconds")
    common.add_argument("--retries", type=int, default=3, help="retries per request")
    common.add_argument("--rate-limit", type=float, default=0.0, metavar="SEC",
                        help="minimum delay between requests")
    common.add_argument("--proxy", help="proxy URL, e.g. http://proxy:3128")
    common.add_argument("--user-agent", default=None, metavar="UA",
                        help="override the User-Agent sent with every request")
    common.add_argument("--browser-fallback", choices=("auto", "on", "off"), default="auto",
                        help="when a request is refused (403), retry through headless Chrome "
                             "to pick up the bot-protection cookies (default: auto)")
    common.add_argument("--browser-engine", choices=("auto", "playwright", "selenium"), default="auto",
                        help="which browser to use for the fallback")
    common.add_argument("--insecure", action="store_true", help="do not verify TLS certificates")
    common.add_argument("-v", "--verbose", action="count", default=0)
    common.add_argument("-q", "--quiet", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sources", parents=[common], help="list the configured sources")
    sub.add_parser("versions", parents=[common], help="show the versions each source publishes")
    sub.add_parser("doctor", parents=[common], help="check connectivity and explain any refusal")

    lister = sub.add_parser("list", parents=[common], help="discover documents without downloading")
    lister.add_argument("--json", action="store_true", help="machine-readable output")

    getter = sub.add_parser("download", parents=[common], help="discover and download")
    getter.add_argument("-o", "--out", type=Path, default=Path("redhat-docs"), help="output directory")
    getter.add_argument("-n", "--only-new", action="store_true",
                        help="fetch only documents that are not already on disk")
    getter.add_argument("-f", "--force", action="store_true", help="re-download everything")
    getter.add_argument("--no-convert", action="store_true",
                        help="never convert HTML to PDF; report those documents as failures")
    getter.add_argument("--pdf-engine", default="auto",
                        choices=("auto", "playwright", "selenium", "weasyprint", "wkhtmltopdf", "none"))
    getter.add_argument("--render-wait", type=float, default=1.5,
                        help="seconds to wait after page load before printing")
    getter.add_argument("--flat", action="store_true", help="put every file in one directory")
    getter.add_argument("--dry-run", action="store_true", help="show what would happen, download nothing")
    getter.add_argument("--prune-manifest", action="store_true",
                        help="drop manifest entries whose files are gone")
    getter.add_argument("--report", type=Path, metavar="FILE", help="write a JSON run report")
    return parser


def parse_version_overrides(values: Sequence[str]) -> Dict[str, str]:
    overrides: Dict[str, str] = {}
    for value in values or []:
        if "=" in value:
            source_id, version = value.split("=", 1)
            overrides[source_id.strip()] = version.strip()
        else:
            overrides["*"] = value.strip()
    return overrides


def make_http(args) -> HttpClient:
    kwargs = dict(
        timeout=args.timeout,
        retries=args.retries,
        rate_limit=args.rate_limit,
        verify_tls=not args.insecure,
        proxy=args.proxy,
        browser_fallback=getattr(args, "browser_fallback", "auto"),
        browser_engine=getattr(args, "browser_engine", "auto"),
    )
    if getattr(args, "user_agent", None):
        kwargs["user_agent"] = args.user_agent
    return HttpClient(**kwargs)


def gather_sources(args):
    catalog = build_catalog(parse_version_overrides(args.version))
    if args.config:
        catalog.update(load_custom_sources(args.config))
    sources = select_sources(catalog, args.source, community=args.community)

    for source in sources:
        if isinstance(source, RedHatDocsSource):
            if args.no_portal_expand:
                source.expand_portal = False
            if args.group_by_category is not None:
                source.group_by_category = args.group_by_category
            if args.category:
                source.categories = compile_patterns(args.category)
            if args.include_product:
                source.include_products = compile_patterns(args.include_product)
            if args.exclude_product:
                source.exclude_products = compile_patterns(args.exclude_product)
        elif isinstance(source, MkDocsSource):
            source.mode = args.community_mode
            source.fingerprint = not args.no_fingerprint
    return catalog, sources


def discover(args, http, sources) -> List[Doc]:
    include = compile_patterns(args.include)
    exclude = compile_patterns(args.exclude)
    docs: List[Doc] = []
    seen = set()
    for source in sources:
        LOG.info("discovering %s ...", source.id)
        try:
            found = source.discover(http, workers=args.workers)
        except Exception as exc:
            LOG.error("[%s] discovery failed: %s", source.id, exc)
            LOG.debug("", exc_info=True)
            continue
        for doc in found:
            if doc.key in seen:
                continue
            haystack = f"{doc.product} {doc.version} {doc.category} {doc.title} {doc.filename}"
            if include and not matches_any(haystack, include):
                continue
            if matches_any(haystack, exclude):
                continue
            seen.add(doc.key)
            docs.append(doc)
    docs.sort(key=lambda d: (d.source_id, d.product, d.version, d.title))
    return docs


# -- commands -------------------------------------------------------------
def cmd_sources(args) -> int:
    catalog, selected = gather_sources(args)
    chosen = {s.id for s in selected}
    print(f"{'id':16} {'kind':8} {'version':10} {'sel':4} label")
    print("-" * 78)
    for source in catalog.values():
        version = getattr(source, "requested_version", "-")
        mark = "*" if source.id in chosen else ""
        print(f"{source.id:16} {source.kind:8} {version:10} {mark:4} {source.label}")
    print("\n* = selected by the current options. Use --source ID (or all/redhat/community).")
    return 0


def cmd_versions(args) -> int:
    _, sources = gather_sources(args)
    http = make_http(args)
    try:
        for source in sources:
            if isinstance(source, RedHatDocsSource):
                info = source.resolve_product(http, source.product, source.requested_version)
                if not info:
                    print(f"{source.id:16} (unavailable)")
                    continue
                others = ", ".join(info.available_versions[:12]) or "-"
                print(f"{source.id:16} current={info.version:8} available: {others}")
            elif isinstance(source, MkDocsSource):
                resolved = source.resolve_version(http)
                concrete = source.display_version(http, resolved)
                extra = f" (build {concrete})" if concrete != resolved else ""
                print(f"{source.id:16} current={resolved}{extra}")
    finally:
        http.close()
    return 0


def cmd_doctor(args) -> int:
    """Explain *why* the site is refusing us: proxy, bot protection, or a bad URL."""
    _, sources = gather_sources(args)
    http = make_http(args)
    probes = []
    for source in sources:
        if isinstance(source, RedHatDocsSource):
            version = source.requested_version or "latest"
            probes.append((source.id, f"https://docs.redhat.com/en/documentation/{source.product}/{version}"))
        elif isinstance(source, MkDocsSource):
            probes.append((source.id, f"{source.base_url(source.requested_version)}/"))
    if not probes:
        probes = [("docs.redhat.com", "https://docs.redhat.com/en/documentation/red_hat_ai/3")]

    worst = 0
    try:
        for source_id, url in probes:
            result = http.diagnose(url)
            mark = "ok  " if result.ok else "FAIL"
            print(f"[{mark}] {source_id:16} {result.status or '-':>4}  {result.verdict}")
            if not result.ok:
                worst = 2
                if result.detail:
                    print(f"                          {result.detail}")
                if result.server:
                    print(f"                          server: {result.server}")
                if result.body_snippet:
                    print(f"                          body: {result.body_snippet[:160]}")

        if worst:
            print("\nthings to try, in order:")
            print("  1. open the same URL in a browser on this machine - if that also fails,")
            print("     it is your network, not the site")
            print("  2. behind a corporate proxy:  --proxy http://proxy:3128  (or set HTTPS_PROXY)")
            print("  3. bot protection:            --browser-fallback on  (needs playwright or selenium)")
            print("  4. TLS interception:          --insecure   (only if you trust the network)")
    finally:
        http.close()
    return worst


def cmd_list(args) -> int:
    _, sources = gather_sources(args)
    http = make_http(args)
    try:
        docs = discover(args, http, sources)
    finally:
        http.close()

    if args.json:
        print(json.dumps([{
            "key": d.key, "source": d.source_id, "product": d.product, "version": d.version,
            "title": d.title, "category": d.category, "filename": d.filename, "page_url": d.page_url,
            "pdf_url": d.pdf_url, "pages_to_render": len(d.render_urls), "note": d.note,
        } for d in docs], indent=2))
        return 0

    if not docs:
        print("no documents discovered")
        return 1
    current = None
    for doc in docs:
        header = f"{doc.product} {doc.version}"
        if header != current:
            current = header
            print(f"\n{header}  [{doc.source_id}]")
        marker = "pdf " if doc.pdf_url else "conv"
        suffix = f"   [{doc.category}]" if doc.category else ""
        print(f"  {marker}  {doc.title}{suffix}")
        LOG.debug("        %s", doc.pdf_url or doc.page_url)
    print(f"\n{len(docs)} document(s) from {len({d.source_id for d in docs})} source(s)")
    return 0


def cmd_download(args) -> int:
    _, sources = gather_sources(args)
    http = make_http(args)
    out_dir: Path = args.out.expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = Manifest(out_dir)
    if args.prune_manifest:
        removed = manifest.forget_missing()
        if removed:
            LOG.info("dropped %d stale manifest entr%s", removed, "y" if removed == 1 else "ies")

    options = Options(
        out_dir=out_dir,
        only_new=args.only_new,
        force=args.force,
        convert=not args.no_convert and args.pdf_engine != "none",
        dry_run=args.dry_run,
        flat=args.flat,
        workers=args.workers,
        pdf_engine=args.pdf_engine,
        render_wait=args.render_wait,
    )

    try:
        docs = discover(args, http, sources)
        if not docs:
            LOG.error("nothing discovered - check the source ids and versions")
            return 1
        LOG.info("%d document(s) to process into %s", len(docs), out_dir)
        results = Downloader(http, manifest, options).run(docs)
    finally:
        http.close()

    counts = summarise(results)
    order = ["downloaded", "converted", "unchanged", "skipped", "planned", "failed"]
    line = "  ".join(f"{name}={counts[name]}" for name in order if name in counts)
    total_bytes = sum(r.size or 0 for r in results if r.status in (DOWNLOADED, CONVERTED))
    print(f"\n{line}   ({human_size(total_bytes)} written to {out_dir})")

    failures = [r for r in results if r.status == FAILED]
    if failures:
        print("\nfailed:")
        for result in failures[:25]:
            print(f"  - {result.doc.label}: {result.message}")
        if len(failures) > 25:
            print(f"  ... and {len(failures) - 25} more")

    if args.report:
        report = {
            "out_dir": str(out_dir),
            "counts": counts,
            "bytes": total_bytes,
            "documents": [{
                "key": r.doc.key, "source": r.doc.source_id, "product": r.doc.product,
                "version": r.doc.version, "title": r.doc.title, "status": r.status,
                "path": str(r.path) if r.path else None, "message": r.message,
            } for r in results],
        }
        try:
            args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
            LOG.info("report written to %s", args.report)
        except OSError as exc:
            LOG.warning("could not write report: %s", exc)

    return 0 if not failures else 2


COMMANDS = {
    "sources": cmd_sources,
    "versions": cmd_versions,
    "doctor": cmd_doctor,
    "list": cmd_list,
    "download": cmd_download,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose, args.quiet)
    try:
        return COMMANDS[args.command](args)
    except KeyboardInterrupt:
        LOG.warning("interrupted")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
