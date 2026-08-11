"""Small shared helpers: logging setup, filename hygiene, version handling."""

from __future__ import annotations

import logging
import re
import sys
from typing import Iterable, List, Optional, Tuple

LOG = logging.getLogger("docsync")

# Tokens that Red Hat capitalises in a specific way. Used only when we have to
# guess a product display name from a URL slug (the page <title> is preferred).
_SPECIAL_TOKENS = {
    "ai": "AI",
    "api": "API",
    "cli": "CLI",
    "hat": "Hat",
    "ibm": "IBM",
    "jboss": "JBoss",
    "llm": "LLM",
    "mcp": "MCP",
    "openshift": "OpenShift",
    "openstack": "OpenStack",
    "red": "Red",
    "rhel": "RHEL",
    "rhoai": "RHOAI",
    "sso": "SSO",
    "vllm": "vLLM",
}


class _ColourFormatter(logging.Formatter):
    COLOURS = {
        logging.DEBUG: "\033[90m",
        logging.INFO: "",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[1;31m",
    }
    RESET = "\033[0m"

    def __init__(self, use_colour: bool) -> None:
        super().__init__("%(message)s")
        self.use_colour = use_colour

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        if record.levelno >= logging.WARNING:
            msg = f"{record.levelname.lower()}: {msg}"
        if self.use_colour:
            colour = self.COLOURS.get(record.levelno, "")
            if colour:
                return f"{colour}{msg}{self.RESET}"
        return msg


def setup_logging(verbosity: int = 0, quiet: bool = False) -> None:
    """verbosity: 0=INFO, 1=DEBUG. quiet forces WARNING."""
    level = logging.WARNING if quiet else (logging.DEBUG if verbosity > 0 else logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_ColourFormatter(use_colour=sys.stderr.isatty()))
    root = logging.getLogger("docsync")
    root.handlers[:] = [handler]
    root.setLevel(level)
    root.propagate = False
    # Third-party libraries are noisy at DEBUG.
    for noisy in ("urllib3", "selenium", "WDM", "weasyprint", "fontTools", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def clean_segment(text: str) -> str:
    """Turn a title fragment into the form Red Hat uses inside PDF filenames.

    Spaces become underscores; trademark symbols are dropped; hyphens and dots
    are preserved ("Self-Managed" stays "Self-Managed").
    """
    text = text.replace("\u00ae", "").replace("\u2122", "").replace("\u2019", "'")
    text = text.replace("\u2013", "-").replace("\u2014", "-").replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace(" ", "_")
    text = re.sub(r"[^\w\-.]", "", text, flags=re.UNICODE)
    return re.sub(r"_+", "_", text).strip("_")


def safe_filename(name: str, fallback: str = "document") -> str:
    """Filesystem-safe name that still reads like the original."""
    name = name.replace("/", "-").replace("\\", "-")
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "", name).strip(" .")
    name = re.sub(r"\s+", " ", name)
    return name[:180] or fallback


def prettify_slug(slug: str) -> str:
    """'red_hat_openshift_ai_self-managed' -> 'Red_Hat_OpenShift_AI_Self-Managed'."""
    words = []
    for word in slug.split("_"):
        parts = []
        for piece in word.split("-"):
            low = piece.lower()
            parts.append(_SPECIAL_TOKENS.get(low, piece.capitalize()))
        words.append("-".join(parts))
    return "_".join(words)


_VERSION_TOKEN = re.compile(r"\d+|[a-zA-Z]+")


def version_key(version: str) -> Tuple:
    """Sortable key. Numeric parts sort numerically, 'x'/'latest' sort high."""
    if not version:
        return (-1,)
    key: List[Tuple[int, object]] = []
    for token in _VERSION_TOKEN.findall(version):
        if token.isdigit():
            key.append((1, int(token)))
        elif token.lower() in ("x", "latest", "stable", "current"):
            key.append((2, 0))
        else:
            key.append((0, token.lower()))
    return tuple(key)


def looks_like_version(text: str) -> bool:
    return bool(re.fullmatch(r"\d+(\.\d+)*(\.x)?(-\w+)?", text.strip()))


def pick_latest(versions: Iterable[str]) -> Optional[str]:
    numeric = [v for v in versions if re.match(r"^\d", v.strip())]
    pool = numeric or [v for v in versions]
    if not pool:
        return None
    return max(pool, key=version_key)


def human_size(num: Optional[int]) -> str:
    if not num:
        return "-"
    step = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if step < 1024 or unit == "GB":
            return f"{step:.0f} {unit}" if unit == "B" else f"{step:.1f} {unit}"
        step /= 1024
    return f"{step:.1f} GB"


def dedupe(items: Iterable[str]) -> List[str]:
    seen, out = set(), []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def compile_patterns(patterns: Optional[Iterable[str]]) -> List[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in (patterns or [])]


def matches_any(text: str, patterns: List[re.Pattern]) -> bool:
    return any(p.search(text) for p in patterns)
