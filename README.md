# docsync

[![tests](https://github.com/erkerc/rhoai-docsync/actions/workflows/tests.yml/badge.svg)](https://github.com/erkerc/rhoai-docsync/actions/workflows/tests.yml)
[![python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Keep a local, offline copy of the Red Hat AI documentation set.

`docsync` discovers every guide published for Red Hat OpenShift AI, Red Hat AI Inference,
Red Hat Connectivity Link and — optionally — the Kuadrant and Open Data Hub Models-as-a-Service
community docs, then downloads them as PDFs. It prefers the PDFs Red Hat already publishes and
renders the HTML itself when a site publishes none.

Useful when you are on a customer site with no connectivity, preparing a workshop, or want a
snapshot of what a given product version said at a point in time.

```
$ docsync download --community --out ~/redhat-docs --only-new
discovering rhai ...
[rhai] Red Hat AI 3
[rhai]   + Red Hat OpenShift AI Self-Managed 3.5 (28 guides)
[rhai]   + Red Hat AI Inference Server 3.2 (9 guides)
  [ok]      Red_Hat_OpenShift_AI_Self-Managed-3.5-Release_notes-en-US.pdf (1.4 MB)
  [convert] Kuadrant-1.5.x-Documentation-en-US.pdf (11.2 MB, 148 page(s))

downloaded=37  converted=2  unchanged=12  skipped=4   (86.1 MB written to ~/redhat-docs)
```

## Features

- **Version aware** — `latest` resolves to the concrete version (`3.5`), or pin one per source
- **PDF first** — uses the official Red Hat PDF build, keeping the official filename
- **Converts when it must** — community sites and PDF-less guides are rendered and merged into
  one bookmarked PDF per doc set
- **Incremental** — a manifest tracks ETag / `Last-Modified` / SHA-256, so a re-run pulls only
  what actually changed; `--only-new` skips everything already on disk
- **Honest about failures** — every candidate URL is verified before it is saved, one bad guide
  never aborts the run, and the exit code reflects the outcome
- **Extensible** — any `docs.redhat.com` product or MkDocs site can be added in JSON, no code
  changes

## Install

```bash
git clone https://github.com/erkerc/rhoai-docsync.git
cd rhoai-docsync
pip install -r requirements.txt      # core
# or: pip install -e .               # also installs a `docsync` command
```

Conversion needs a rendering engine. Pick one — only community sites and PDF-less guides need it:

```bash
pip install playwright && playwright install chromium   # best fidelity, recommended
pip install selenium webdriver-manager                  # headless Chrome
pip install weasyprint                                  # no browser, no JavaScript
```

If you only want the Red Hat PDFs, skip all three and pass `--no-convert`.

## Usage

```bash
python -m docsync doctor                       # can we reach the sites at all?
python -m docsync sources                      # what is configured
python -m docsync versions --source rhoai      # which versions exist
python -m docsync list --source rhcl           # discover, download nothing
python -m docsync download --out ~/redhat-docs # the default set
```

Common runs:

```bash
# Everything Red Hat plus the two community sites
python -m docsync download --community --out ~/redhat-docs

# Pin versions: one source, or all of them
python -m docsync download --source rhoai --version 3.5 --out ~/redhat-docs
python -m docsync download --source rhoai,rhcl --version rhoai=3.5 --version rhcl=1.4 --out ~/redhat-docs

# Only fetch what is not already there (nightly cron)
python -m docsync download --community --only-new --out ~/redhat-docs

# Refresh: re-checks ETags and pulls only what changed on the server
python -m docsync download --out ~/redhat-docs

# Just the serving and inference guides
python -m docsync download --include 'serving|inference|llm-d|models-as-a-service' --out ~/redhat-docs

# Kuadrant as one PDF per page instead of one merged book
python -m docsync download --source kuadrant --community-mode per-page --out ~/redhat-docs

# See the plan without downloading anything
python -m docsync download --community --dry-run
```

### Sources

| id | what |
|---|---|
| `rhai` | `red_hat_ai/3` portfolio index — a portal page, so linked products are followed one level deep |
| `rhoai` | Red Hat OpenShift AI Self-Managed |
| `rhoai-cloud` | Red Hat OpenShift AI Cloud Service |
| `rhaiis` | Red Hat AI Inference Server |
| `rhai-inference` | Red Hat AI Inference |
| `rhelai` | Red Hat Enterprise Linux AI |
| `rhcl` | Red Hat Connectivity Link |
| `maas` | Open Data Hub Models-as-a-Service (community) |
| `kuadrant` | Kuadrant (community) |

`--source` also accepts `all`, `redhat`, `community` and `default`. Add your own products in a
JSON file (see [`sources.example.json`](sources.example.json)) and pass `--config sources.json`.

### Incremental behaviour

A manifest (`.docsync-manifest.json`) is written in the output directory recording the URL,
size, SHA-256, ETag and `Last-Modified` of every file.

| flag | behaviour |
|---|---|
| *(default)* | conditional `GET`; the server answers `304` for unchanged files and nothing is re-downloaded |
| `--only-new` | anything already recorded and still on disk is skipped without a request |
| `--force` | re-fetch everything |
| `--prune-manifest` | drop records whose files you deleted, so they are fetched again |

Community sites are checked with a fingerprint built from every page's ETag, so a merged PDF is
only rebuilt when a page actually changed. Delete a PDF and re-run: it comes back. Delete the
manifest: files are re-verified but not duplicated.

### Output layout

Files keep the official Red Hat filenames. `--flat` puts everything in one directory instead.

```
redhat-docs/
├── .docsync-manifest.json
├── red_hat_openshift_ai_self-managed/3.5/Red_Hat_OpenShift_AI_Self-Managed-3.5-Release_notes-en-US.pdf
├── red_hat_connectivity_link/1.4/Red_Hat_Connectivity_Link-1.4-Release_notes-en-US.pdf
└── kuadrant/1.5.x/Kuadrant-1.5.x-Documentation-en-US.pdf
```

## How discovery works

**docs.redhat.com** pages are server-rendered, so plain HTTP is enough — no browser is needed
for discovery.

1. `…/documentation/<product>/<version>` is fetched and its `<title>` gives the product's
   display name and concrete version (this is how `latest` becomes `3.5`).
2. Every `…/html/<guide>` and `…/html-single/<guide>` link for that product becomes a guide.
   On a portal page such as `red_hat_ai/3`, links pointing at *other* products are followed
   once more, so one source id covers the whole portfolio (`--no-portal-expand` turns this off).
3. For each guide the PDF URL is resolved in this order: a `/pdf/<guide>/…pdf` link published
   on the page itself, then constructed candidates `Product-Version-Guide-en-US.pdf` built from
   the page title, the H1 and the URL slug.
4. Each candidate is probed before download. A wrong filename gets a `302` to a landing page
   that then answers `200 text/html`, so the probe checks the content type, the final URL after
   redirects, and the `%PDF-` magic bytes — a bare status check would happily save an HTML
   error page as a `.pdf`.
5. If no candidate holds up, the `html-single` version of the guide is rendered instead.

**MkDocs sites** (Kuadrant, MaaS) are versioned with `mike`: `versions.json` resolves aliases
like `latest`, `sitemap.xml` lists the pages, and the nav on the landing page supplies reading
order. Pages are rendered individually and merged with a bookmark per page.

## Reliability

- retries with exponential backoff on 408/425/429 and 5xx, honouring `Retry-After`
- downloads stream to a `.part` file and are renamed only after the magic-byte and length
  checks pass, so an interrupted run never leaves a half-written PDF in place
- one failed guide never aborts the run; failures are listed at the end
- exit codes: `0` clean, `2` something failed, `130` interrupted
- `--report run.json` writes a machine-readable summary for cron/CI
- `--rate-limit 0.5 --workers 2` to be gentle on the server; `--proxy` and `--insecure` for
  corporate networks

## Troubleshooting

Start with `python -m docsync doctor` — it fetches one URL per selected source and says whether
a refusal came from a proxy, from the site's bot protection, or from a wrong URL.

**Everything returns 403.** Two very different causes:

- *your network* — a corporate proxy or TLS-inspecting gateway sits in front of you. The
  response usually carries proxy headers or a block page; `doctor` names it. Fix with
  `--proxy http://proxy:3128` (or `HTTPS_PROXY=...`), and `--insecure` if the gateway re-signs
  certificates.
- *the site* — the CDN in front of `docs.redhat.com` sometimes refuses scripted clients.
  docsync sends a full browser header set, and on a 403 it opens the site once in headless
  Chrome, copies the resulting cookies into the HTTP session and retries. Force it with
  `--browser-fallback on` (needs `playwright` or `selenium`); if even that is refused, the page
  HTML and PDF bytes are pulled straight out of the browser. Disable it with
  `--browser-fallback off`, or set your own identity with `--user-agent '...'`.

| symptom | cause / fix |
|---|---|
| every URL returns 403 | run `docsync doctor`; then `--proxy`, or `--browser-fallback on` |
| `no PDF found and conversion is disabled` | guide has no published PDF; drop `--no-convert` and install a render engine |
| `no rendering engine installed` | `pip install playwright && playwright install chromium` |
| every guide fails with `redirected to a non-PDF page` | the version probably does not exist — check `docsync versions --source <id>` |
| discovery returns 0 documents | wrong product slug or version; `docsync list -v` prints the URLs it tried |
| Selenium cannot start Chrome | install Chrome/Chromium, or use `--pdf-engine playwright` |
| slow community runs | `--no-fingerprint` skips the per-page change check |

## Development

```bash
python tests/test_offline.py         # discovery, filename construction, manifest, CLI
python tests/test_download_local.py  # probing, downloads, 304s, --only-new, --force
```

Both suites run without network access; the second starts a throwaway HTTP server that
reproduces the awkward cases (HTML served as `.pdf`, `304 Not Modified`, missing files).

Layout:

```
docsync/
├── cli.py              argument parsing, the sources/versions/doctor/list/download commands
├── catalog.py          built-in source definitions, JSON config loading
├── downloader.py       plan → download in parallel → convert what is left → manifest
├── http_client.py      retries, redirect-aware PDF probing, atomic downloads, diagnostics
├── browser.py          headless Chrome, used only when plain HTTP is refused
├── pdf_render.py       Playwright / Selenium / WeasyPrint / wkhtmltopdf, plus PDF merging
├── manifest.py         on-disk state for incremental runs
└── sources/
    ├── redhat.py       docs.redhat.com discovery and PDF URL resolution
    └── mkdocs_site.py  MkDocs Material sites versioned with mike
```

Adding a product needs no code — a JSON entry is enough. Adding a new *kind* of site means a
new class in `sources/` implementing `discover(http, workers) -> list[Doc]`.

Issues and pull requests welcome.

## Notes

This is a personal project and is not affiliated with, endorsed by, or supported by Red Hat.
It only fetches documentation that is already public, at a polite request rate. The downloaded
documents remain the copyright of their respective authors — keep them for your own offline use
and follow the [Red Hat terms of use](https://www.redhat.com/en/about/terms-use) rather than
redistributing them.

## License

[MIT](LICENSE)
