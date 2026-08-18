"""Built-in sources, plus loading of user-defined ones from JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .sources import MkDocsSource, RedHatDocsSource, Source
from .util import LOG

# Products that a portal page links to but that are rarely wanted alongside the
# AI stack. Override with --include-product / --exclude-product.
PORTAL_EXCLUDES = [r"^red_hat_enterprise_linux$", r"^openshift_container_platform$"]


def build_catalog(versions: Optional[Dict[str, str]] = None) -> Dict[str, Source]:
    """id -> Source. `versions` overrides the default version per source id."""
    versions = versions or {}

    def ver(source_id: str, default: str) -> str:
        return versions.get(source_id, versions.get("*", default))

    catalog: List[Source] = [
        # --- Red Hat AI portfolio -------------------------------------------------
        RedHatDocsSource(
            id="rhai",
            product="red_hat_ai",
            version=ver("rhai", "3"),
            label="Red Hat AI (portfolio index)",
            portal=True,
            exclude_products=PORTAL_EXCLUDES,
        ),
        RedHatDocsSource(
            id="rhoai",
            product="red_hat_openshift_ai_self-managed",
            version=ver("rhoai", "latest"),
            label="Red Hat OpenShift AI Self-Managed",
        ),
        RedHatDocsSource(
            id="rhoai-cloud",
            product="red_hat_openshift_ai_cloud_service",
            version=ver("rhoai-cloud", "1"),
            label="Red Hat OpenShift AI Cloud Service",
        ),
        RedHatDocsSource(
            id="rhaiis",
            product="red_hat_ai_inference_server",
            version=ver("rhaiis", "latest"),
            label="Red Hat AI Inference Server",
        ),
        RedHatDocsSource(
            id="rhai-inference",
            product="red_hat_ai_inference",
            version=ver("rhai-inference", "latest"),
            label="Red Hat AI Inference",
        ),
        RedHatDocsSource(
            id="rhelai",
            product="red_hat_enterprise_linux_ai",
            version=ver("rhelai", "latest"),
            label="Red Hat Enterprise Linux AI",
        ),
        # --- Connectivity Link ----------------------------------------------------
        RedHatDocsSource(
            id="rhcl",
            product="red_hat_connectivity_link",
            version=ver("rhcl", "1.4"),
            label="Red Hat Connectivity Link",
        ),
        # --- OpenShift ------------------------------------------------------------
        # ~100 guides per version, so grouped into category sub-directories by
        # default and kept out of DEFAULT_SOURCES - ask for it explicitly.
        RedHatDocsSource(
            id="ocp",
            product="openshift_container_platform",
            version=ver("ocp", "latest"),
            label="OpenShift Container Platform",
            group_by_category=True,
        ),
        RedHatDocsSource(
            id="rosa",
            product="red_hat_openshift_service_on_aws",
            version=ver("rosa", "latest"),
            label="Red Hat OpenShift Service on AWS (ROSA)",
            group_by_category=True,
        ),
        RedHatDocsSource(
            id="ocp-virt",
            product="red_hat_openshift_virtualization",
            version=ver("ocp-virt", "latest"),
            label="Red Hat OpenShift Virtualization",
        ),

        # --- Community ------------------------------------------------------------
        MkDocsSource(
            id="maas",
            label="ODH Models-as-a-Service",
            root_url="https://opendatahub-io.github.io/models-as-a-service",
            version=ver("maas", "latest"),
        ),
        MkDocsSource(
            id="kuadrant",
            label="Kuadrant",
            root_url="https://docs.kuadrant.io",
            version=ver("kuadrant", "1.5.x"),
        ),
    ]
    return {source.id: source for source in catalog}


#: Downloaded when no --source is given.
DEFAULT_SOURCES = ["rhai", "rhoai", "rhaiis", "rhai-inference", "rhcl"]
#: Added by --community.
COMMUNITY_SOURCES = ["maas", "kuadrant"]
#: Selected by --source openshift. Large - not part of the default set.
OPENSHIFT_SOURCES = ["ocp", "rosa", "ocp-virt"]


def select_sources(
    catalog: Dict[str, Source],
    requested: Optional[Sequence[str]] = None,
    community: bool = False,
) -> List[Source]:
    """Resolve --source values (ids, 'all', 'community', 'redhat') to sources."""
    if not requested:
        ids = list(DEFAULT_SOURCES)
        if community:
            ids += COMMUNITY_SOURCES
        return [catalog[i] for i in ids if i in catalog]

    ids: List[str] = []
    for token in requested:
        for part in str(token).split(","):
            part = part.strip()
            if not part:
                continue
            if part == "all":
                ids += [s.id for s in catalog.values()]
            elif part == "community":
                ids += [s.id for s in catalog.values() if s.community]
            elif part == "openshift":
                ids += OPENSHIFT_SOURCES
            elif part == "redhat":
                ids += [s.id for s in catalog.values() if not s.community]
            elif part == "default":
                ids += DEFAULT_SOURCES
            elif part in catalog:
                ids.append(part)
            else:
                LOG.warning("unknown source %r (see: docsync sources)", part)
    if community:
        ids += COMMUNITY_SOURCES

    seen, out = set(), []
    for source_id in ids:
        if source_id in catalog and source_id not in seen:
            seen.add(source_id)
            out.append(catalog[source_id])
    return out


def load_custom_sources(path: Path) -> Dict[str, Source]:
    """Read extra sources from a JSON file.

    [
      {"type": "redhat", "id": "rhbk", "product": "red_hat_build_of_keycloak",
       "version": "26.0", "label": "Keycloak"},
      {"type": "mkdocs", "id": "llm-d", "label": "llm-d",
       "root_url": "https://llm-d.ai/docs", "version": "latest"}
    ]
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOG.error("cannot read source config %s: %s", path, exc)
        return {}

    sources: Dict[str, Source] = {}
    for raw in data if isinstance(data, list) else data.get("sources", []):
        kind = str(raw.pop("type", "redhat")).lower()
        try:
            if kind == "redhat":
                source = RedHatDocsSource(**raw)
            elif kind == "mkdocs":
                source = MkDocsSource(**raw)
            else:
                LOG.warning("unknown source type %r in %s", kind, path)
                continue
        except TypeError as exc:
            LOG.warning("bad source definition in %s: %s", path, exc)
            continue
        sources[source.id] = source
    LOG.debug("loaded %d custom source(s) from %s", len(sources), path)
    return sources
