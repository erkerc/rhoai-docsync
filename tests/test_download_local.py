"""End-to-end check of probing, downloading and incremental behaviour.

Runs against a throwaway HTTP server on localhost, so no internet is needed.
Run with: python tests/test_download_local.py
"""

import http.server
import socketserver
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docsync.downloader import Downloader, Options
from docsync.http_client import HttpClient
from docsync.manifest import Manifest
from docsync.sources.base import Doc
from docsync.util import setup_logging

PDF_BYTES = b"%PDF-1.7\n% fake but well formed enough\n1 0 obj<</Type/Catalog>>endobj\ntrailer\n%%EOF\n"


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _route(self):
        if self.path.endswith("/good.pdf"):
            return 200, "application/pdf", PDF_BYTES
        if self.path.endswith("/html-instead.pdf"):
            # What docs.redhat.com does for a filename that does not exist.
            return 200, "text/html; charset=utf-8", b"<html><body>Page not found</body></html>"
        if self.path.endswith("/missing.pdf"):
            return 404, "text/plain", b"nope"
        return 404, "text/plain", b"nope"

    def do_HEAD(self):
        status, ctype, body = self._route()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", '"v1"')
        self.end_headers()

    def do_GET(self):
        status, ctype, body = self._route()
        if self.headers.get("If-None-Match") == '"v1"' and status == 200:
            self.send_response(304)
            self.end_headers()
            return
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", '"v1"')
        self.end_headers()
        self.wfile.write(body)


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}{('  ' + str(detail)) if not condition else ''}")
    return bool(condition)


def make_doc(key, filename, candidates, render=True):
    return Doc(
        key=key, source_id="test", product="Test Product", version="1.0",
        title=key, filename=filename, rel_dir="test/1.0",
        page_url="http://example.invalid/page", pdf_url=candidates[0] if candidates else None,
        pdf_candidates=list(candidates),
        render_urls=["http://example.invalid/page"] if render else [],
    )


def main():
    setup_logging(quiet=True)
    ok = True
    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"
        http_client = HttpClient(timeout=5, retries=0)

        # --- probing ------------------------------------------------------
        ok &= check("real PDF probes ok", http_client.probe_pdf(f"{base}/good.pdf").ok)
        html_probe = http_client.probe_pdf(f"{base}/html-instead.pdf")
        ok &= check("HTML served as .pdf is rejected", not html_probe.ok, html_probe.reason)
        ok &= check("404 is rejected", not http_client.probe_pdf(f"{base}/missing.pdf").ok)

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            manifest = Manifest(out)
            options = Options(out_dir=out, workers=2, convert=False)
            downloader = Downloader(http_client, manifest, options)

            docs = [
                make_doc("ok", "good.pdf", [f"{base}/missing.pdf", f"{base}/good.pdf"]),
                make_doc("bad", "bad.pdf", [f"{base}/html-instead.pdf"], render=False),
            ]
            results = {r.doc.key: r for r in downloader.run(docs)}
            ok &= check("falls through to the second candidate", results["ok"].status == "downloaded",
                        results["ok"].message)
            ok &= check("file landed on disk", (out / "test/1.0/good.pdf").read_bytes() == PDF_BYTES)
            ok &= check("no .part left behind", not list(out.rglob("*.part")))
            ok &= check("undownloadable doc reported as failed", results["bad"].status == "failed",
                        results["bad"].status)
            ok &= check("manifest recorded the good file", manifest.known("ok"))

            # --- second run: conditional GET ------------------------------
            again = {r.doc.key: r for r in Downloader(http_client, Manifest(out), options).run(docs)}
            ok &= check("re-run reports unchanged (304)", again["ok"].status == "unchanged",
                        again["ok"].status)

            # --- only-new -------------------------------------------------
            only_new = Options(out_dir=out, workers=2, convert=False, only_new=True)
            skipped = {r.doc.key: r for r in Downloader(http_client, Manifest(out), only_new).run(docs)}
            ok &= check("--only-new skips existing files", skipped["ok"].status == "skipped")

            # --- force ----------------------------------------------------
            forced = Options(out_dir=out, workers=2, convert=False, force=True)
            refetched = {r.doc.key: r for r in Downloader(http_client, Manifest(out), forced).run(docs)}
            ok &= check("--force re-downloads", refetched["ok"].status == "downloaded",
                        refetched["ok"].status)

            # --- dry run --------------------------------------------------
            dry = Options(out_dir=out / "dry", workers=2, convert=False, dry_run=True)
            planned = {r.doc.key: r for r in Downloader(http_client, Manifest(out / "dry"), dry).run(docs)}
            ok &= check("--dry-run writes nothing", planned["ok"].status == "planned"
                        and not (out / "dry" / "test").exists())

        http_client.close()
        server.shutdown()

    print("\nALL PASSED" if ok else "\nSOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
