# utils.py
import os
import re
import logging
import traceback
from typing import Optional, Iterable, Tuple, List
from rdflib import Graph, RDF, RDFS, OWL, URIRef, Literal, BNode
from rdflib.namespace import DC, DCTERMS, SKOS, SH, VANN
from collections import defaultdict

log = logging.getLogger("ttl2mkdocs")

# -------------------- namespaces --------------------
DESC_PROPS = (DC.description, SKOS.definition, RDFS.comment, DCTERMS.description)
SKIP_IN_OTHER = set(DESC_PROPS) | {RDFS.label, DCTERMS.description, SKOS.note, SKOS.example}


def load_oasis_catalog(catalog_path: str) -> dict:
    """
    Parse an OASIS XML catalog into {name IRI -> local path or URL}.

    Relative catalog `uri` values are resolved against the catalog file's directory.
    `file:` URIs are converted to filesystem paths. Supports `<uri name="..." uri="..."/>`
    entries (with or without XML namespace).
    """
    import xml.etree.ElementTree as ET
    from urllib.parse import urlparse
    from urllib.request import url2pathname

    mapping = {}
    if not catalog_path or not os.path.isfile(catalog_path):
        return mapping
    try:
        tree = ET.parse(catalog_path)
    except Exception as e:
        log.warning("Could not parse catalog %s: %s", catalog_path, e)
        return mapping

    catalog_dir = os.path.dirname(os.path.abspath(catalog_path))
    root = tree.getroot()
    for el in root.iter():
        if not str(el.tag).endswith("uri"):
            continue
        name = el.get("name")
        href = el.get("uri")
        if not name or not href:
            continue
        if href.startswith("file:"):
            target = url2pathname(urlparse(href).path)
        elif os.path.isabs(href) or "://" in href:
            target = href
        else:
            target = os.path.normpath(os.path.join(catalog_dir, href))
        mapping[name] = target
        mapping[name.rstrip("/")] = target
        if not name.endswith("/"):
            mapping[name + "/"] = target
    log.info("Loaded catalog URI mappings from %s (%d names)", catalog_path, len({k.rstrip("/") for k in mapping}))
    return mapping


def discover_oasis_catalogs(search_roots: Optional[Iterable[str]] = None) -> dict:
    """
    Merge OASIS catalogs found in common locations (later files override earlier).

    Search order (portable):
      - $ONT2MD_CATALOG if set (relative paths resolved from cwd)
      - ./catalog-v001.xml, ./catalog.xml
      - ./docs/catalog-v001.xml, ./docs/catalog.xml
      - plus the same under each path in search_roots
    """
    merged = {}
    candidates = []
    env = os.environ.get("ONT2MD_CATALOG")
    if env:
        candidates.append(
            os.path.normpath(os.path.join(os.getcwd(), env)) if not os.path.isabs(env) else env
        )
    base_roots = [os.getcwd(), os.path.join(os.getcwd(), "docs")]
    if search_roots:
        base_roots.extend(search_roots)
    for root in base_roots:
        for name in ("catalog-v001.xml", "catalog.xml"):
            candidates.append(os.path.join(root, name))

    seen = set()
    for path in candidates:
        if not (os.path.isfile(path) and path.lower().endswith(".xml")):
            continue
        ap = os.path.abspath(path)
        if ap in seen:
            continue
        seen.add(ap)
        merged.update(load_oasis_catalog(ap))
    return merged


def resolve_iri_via_catalog(iri: str, catalog: dict) -> Optional[str]:
    """
    Resolve an IRI via catalog.

    Exact matches always apply. Prefix matches apply only when the catalog
    name is a namespace-style prefix (ends with '/') and the target is a
    directory (or directory-like URI), so a mapping of a whole ontology IRI
    onto a single .owl/.ttl file is not accidentally reused for child IRIs.
    """
    if not catalog:
        return None

    def _usable(target: Optional[str]) -> Optional[str]:
        if not target:
            return None
        # Local path that does not exist → treat as unresolved so callers can
        # fall back to fetching the original IRI (common with machine-specific
        # Protégé file: catalogs checked out elsewhere).
        if "://" not in target and not os.path.exists(target):
            log.debug("Catalog target missing on disk, ignoring: %s", target)
            return None
        return target

    if iri in catalog:
        return _usable(catalog[iri])
    alt = iri.rstrip("/")
    if alt in catalog:
        return _usable(catalog[alt])
    if (alt + "/") in catalog:
        return _usable(catalog[alt + "/"])

    best_name = None
    for name, target in catalog.items():
        if not name.endswith("/"):
            continue
        if not iri.startswith(name):
            continue
        is_dir_target = (
            target.endswith("/")
            or target.endswith(os.sep)
            or os.path.isdir(target)
        )
        if not is_dir_target:
            continue
        if best_name is None or len(name) > len(best_name):
            best_name = name
    if best_name is None:
        return None
    base = catalog[best_name]
    remainder = iri[len(best_name) :]
    if "://" in base:
        return base.rstrip("/") + "/" + remainder.lstrip("/")
    if not remainder:
        return _usable(base)
    return _usable(os.path.normpath(os.path.join(base, remainder)))


def _expand_local_path(path: str, base_dir: Optional[str] = None) -> str:
    path = os.path.expanduser(path.strip())
    if not os.path.isabs(path) and base_dir:
        path = os.path.join(base_dir, path)
    return os.path.normpath(path)


def _entrypoint_in_dir(directory: str, iri: str) -> Optional[str]:
    """Pick a likely ontology entry file under a mapped local directory."""
    docs = os.path.join(directory, "docs") if os.path.isdir(os.path.join(directory, "docs")) else directory
    # Derive a short name from the IRI path (e.g. .../itsdata/vehicle/v1/ → vehicle)
    parts = [p for p in iri.rstrip("/").split("/") if p and p != "v1" and not re.fullmatch(r"v\d+", p)]
    leaf = parts[-1] if parts else ""
    leaf = leaf.replace("part", "") if leaf.startswith("part") and leaf[4:].isdigit() else leaf

    candidates = []
    if leaf:
        for name in (f"its-{leaf}.ttl", f"{leaf}.ttl", f"cdm{leaf}.ttl", f"its-{leaf}.owl", f"{leaf}.owl"):
            candidates.append(os.path.join(docs, name))
    # Common project entrypoints
    for name in ("its-core.ttl", "cdm1.ttl", "cdm2.ttl", "cdm3.ttl"):
        candidates.append(os.path.join(docs, name))

    for p in candidates:
        if os.path.isfile(p):
            return p

    # Last resort: single non-pattern/non-shacl ttl in docs/
    if os.path.isdir(docs):
        ttls = [
            os.path.join(docs, f)
            for f in sorted(os.listdir(docs))
            if f.lower().endswith((".ttl", ".owl", ".rdf"))
            and "-pattern" not in f.lower()
            and "-shacl" not in f.lower()
            and "-reqview" not in f.lower()
        ]
        if len(ttls) == 1:
            return ttls[0]
    return None


def load_dev_iri_map(map_path: str) -> dict:
    """
    Load a per-user IRI → local path map used only with ``ttl2md.py --dev``.

    File formats:
      - YAML object: ``{ "https://.../": "/local/path", ... }``
      - JSON object with the same shape
      - Optional wrapper key ``mappings:``

    Paths may use ``~`` and may be relative to the map file's directory.
    Values may be ontology files or checkout directories (entrypoint is inferred).
    """
    import json

    if not map_path or not os.path.isfile(map_path):
        raise FileNotFoundError(f"Dev IRI map not found: {map_path}")

    raw = open(map_path, "r", encoding="utf-8").read()
    data = None
    lower = map_path.lower()
    if lower.endswith((".yml", ".yaml")):
        try:
            import yaml  # type: ignore
            data = yaml.safe_load(raw)
        except Exception as e:
            raise ValueError(f"Could not parse YAML dev map {map_path}: {e}") from e
    elif lower.endswith(".json"):
        data = json.loads(raw)
    else:
        # Try YAML then JSON
        try:
            import yaml  # type: ignore
            data = yaml.safe_load(raw)
        except Exception:
            data = json.loads(raw)

    if not isinstance(data, dict):
        raise ValueError(f"Dev IRI map must be a mapping object: {map_path}")

    if "mappings" in data and isinstance(data["mappings"], dict):
        data = data["mappings"]

    base_dir = os.path.dirname(os.path.abspath(map_path))
    out: dict = {}
    for name, target in data.items():
        if not name or target is None:
            continue
        if not isinstance(name, str) or not isinstance(target, str):
            log.warning("Ignoring non-string mapping entry in %s: %r → %r", map_path, name, target)
            continue
        path = _expand_local_path(target, base_dir)
        out[name] = path
        out[name.rstrip("/")] = path
        if not name.endswith("/"):
            out[name + "/"] = path
    log.info("Loaded %d IRI keys from dev map %s", len({k.rstrip("/") for k in out}), map_path)
    return out


def resolve_iri_via_dev_map(iri: str, dev_map: dict) -> Optional[str]:
    """Resolve an import IRI through a --dev local map (exact or longest prefix)."""
    if not dev_map:
        return None

    def _usable(target: Optional[str], for_iri: str) -> Optional[str]:
        if not target:
            return None
        if os.path.isfile(target):
            return target
        if os.path.isdir(target):
            entry = _entrypoint_in_dir(target, for_iri)
            if entry:
                return entry
            log.warning("Dev map target is a directory with no ontology entry file: %s", target)
            return None
        log.debug("Dev map target missing on disk: %s", target)
        return None

    if iri in dev_map:
        hit = _usable(dev_map[iri], iri)
        if hit:
            return hit
    alt = iri.rstrip("/")
    for key in (alt, alt + "/"):
        if key in dev_map:
            hit = _usable(dev_map[key], iri)
            if hit:
                return hit

    # Longest prefix match (namespace → directory or file)
    best = None
    for name in dev_map:
        if not name.endswith("/"):
            continue
        if iri.startswith(name) and (best is None or len(name) > len(best)):
            best = name
    if best is None:
        return None
    target = dev_map[best]
    if os.path.isfile(target):
        return target
    if os.path.isdir(target):
        # If prefix maps to a parent of checkouts, remainder may be a relative path
        remainder = iri[len(best) :].strip("/")
        if remainder:
            # try remainder as relative path under target
            candidate = os.path.normpath(os.path.join(target, remainder))
            if os.path.isfile(candidate):
                return candidate
            if os.path.isdir(candidate):
                return _entrypoint_in_dir(candidate, iri)
        return _entrypoint_in_dir(target, iri)
    return None


def resolve_import_iri(
    iri: str,
    catalog: Optional[dict] = None,
    dev_map: Optional[dict] = None,
) -> str:
    """
    Resolve an owl:imports IRI for loading.

    Order: ``--dev`` map (if provided) → OASIS catalog → original IRI (HTTP fetch).
    The dev map is applied only when the caller passes one (ttl2md ``--dev``).
    """
    if dev_map:
        via_dev = resolve_iri_via_dev_map(iri, dev_map)
        if via_dev:
            log.info("Dev map: <%s> → %s", iri, via_dev)
            return via_dev
    if catalog:
        via_cat = resolve_iri_via_catalog(iri, catalog)
        if via_cat:
            return via_cat
    return iri


def find_dev_iri_map_path(
    explicit: Optional[str] = None,
    search_roots: Optional[Iterable[str]] = None,
) -> Optional[str]:
    """
    Locate a per-user dev IRI map file.

    Search order (first hit wins):
      1. Explicit ``--dev-map`` path
      2. Personal map names under: search_roots, cwd, tool install root (ont2md/),
         and ``~/.config/ont2md/``
      3. ``dev-iri-map.example.yml`` in those same places (last resort)

    Personal map names: ``dev-iri-map.yml`` / ``.yaml`` / ``.json``,
    ``.ont2md-dev-map.yml`` / ``.yaml`` / ``.json``.
    """
    if explicit:
        path = os.path.expanduser(explicit)
        return path if os.path.isfile(path) else None

    personal = (
        "dev-iri-map.yml",
        "dev-iri-map.yaml",
        "dev-iri-map.json",
        ".ont2md-dev-map.yml",
        ".ont2md-dev-map.yaml",
        ".ont2md-dev-map.json",
    )
    examples = (
        "dev-iri-map.example.yml",
        "dev-iri-map.example.yaml",
        "dev-iri-map.example.json",
    )

    tool_python = os.path.dirname(os.path.realpath(__file__))
    tool_root = os.path.dirname(tool_python)
    roots: list[str] = []
    if search_roots:
        roots.extend(search_roots)
    roots.extend(
        [
            os.getcwd(),
            tool_root,
            tool_python,
            os.path.join(os.path.expanduser("~"), ".config", "ont2md"),
        ]
    )

    def _scan(names: tuple[str, ...]) -> Optional[str]:
        seen = set()
        for root in roots:
            if not root:
                continue
            for name in names:
                path = os.path.abspath(os.path.join(root, name))
                if path in seen:
                    continue
                seen.add(path)
                if os.path.isfile(path):
                    return path
        return None

    hit = _scan(personal)
    if hit:
        return hit
    return _scan(examples)


def describe_dev_iri_map_search(search_roots: Optional[Iterable[str]] = None) -> list[str]:
    """Return absolute paths that ``find_dev_iri_map_path`` would consider (for error messages)."""
    tool_python = os.path.dirname(os.path.realpath(__file__))
    tool_root = os.path.dirname(tool_python)
    roots: list[str] = []
    if search_roots:
        roots.extend(search_roots)
    roots.extend(
        [
            os.getcwd(),
            tool_root,
            os.path.join(os.path.expanduser("~"), ".config", "ont2md"),
        ]
    )
    names = (
        "dev-iri-map.yml",
        "dev-iri-map.yaml",
        "dev-iri-map.json",
        ".ont2md-dev-map.yml",
        "dev-iri-map.example.yml",
    )
    out = []
    seen = set()
    for root in roots:
        for name in names:
            path = os.path.abspath(os.path.join(root, name))
            if path not in seen:
                seen.add(path)
                out.append(path)
    return out


def _norm_base(u: str) -> str:
    return u.rstrip('/#')

def get_pattern_name(ont_name: str) -> str:
    # Turtle pattern filenames stay lowercase/hyphenated on disk (e.g., area-pattern.ttl),
    # but the *generated pattern page* is controlled separately via ontology_info.module_name.
    return f"{ont_name}-pattern"

def get_shacl_name(ont_name: str) -> str:
    return f"{ont_name}-shacl"

def is_pattern_ttl_file(ont: dict) -> bool:
    """True when the ontology module is loaded from a *-pattern.ttl file."""
    path = ont.get("file") or ""
    return os.path.basename(path).lower().endswith("-pattern.ttl")

def get_source_ttl_basename(ont_name: str, ont: dict) -> str:
    """Return the on-disk Turtle filename for an ontology module."""
    path = ont.get("file")
    if path:
        return os.path.basename(path)
    return get_pattern_name(ont_name) + ".ttl"

def resolve_home_ontology(ontology_info: dict, preferred_prefix: str) -> str | None:
    """
    Pick the ontology module that owns docs/index.md.

    The home page follows vann:preferredNamespacePrefix when a matching TTL file
    exists (e.g. its-time.ttl). Otherwise use the module that declares that
    prefix (e.g. core.ttl with prefix its-time), or the sole remaining module.
    """
    if not ontology_info:
        return None

    pref = (preferred_prefix or "").strip()
    if pref and pref in ontology_info:
        return pref

    for name, ont in ontology_info.items():
        if name.endswith("-reqview"):
            continue
        if pref and ont.get("prefix") == pref:
            return name

    non_reqview = [n for n in ontology_info if not n.endswith("-reqview")]
    if len(non_reqview) == 1:
        return non_reqview[0]
    return non_reqview[0] if non_reqview else None

def should_skip_nav_ontology(ont_name: str, ont: dict) -> bool:
    """Skip nav for ReqView sidecars, alignment/SHACL modules, and empty shells."""
    if ont_name.endswith("-reqview"):
        return True
    path = (ont.get("file") or "").lower()
    if path.endswith("-alignment.ttl") or path.endswith("-shacl.ttl"):
        return True
    if ont_name == ont.get("prefix"):
        classes = ont.get("classes") or set()
        properties = ont.get("properties") or []
        if not classes and not properties:
            return True
    return False

def get_pattern_modules(ontology_info: dict) -> list:
    """Ontology module keys backed by a *-pattern.ttl file."""
    return sorted(
        (
            name
            for name, ont in ontology_info.items()
            if not name.endswith("-reqview") and is_pattern_ttl_file(ont)
        ),
        key=str.lower,
    )

def get_nav_modules(ontology_info: dict) -> list:
    """Ontology modules that should appear in mkdocs navigation."""
    return sorted(
        (
            name
            for name in ontology_info
            if not should_skip_nav_ontology(name, ontology_info[name])
        ),
        key=str.lower,
    )

def get_prefix_named_pairs(ontology_doc, ns: str):
    """Return [{'prefix': <str>, 'uri': <str>}, ...] from funowl PrefixDeclarations,
    handling different return shapes of as_prefixes() across funowl versions."""
    pd = getattr(ontology_doc, "prefixDeclarations", None)
    if not pd:
#        log.debug("No prefix declarations found, using default namespace: %s", ns)
        return [{"prefix": "", "uri": ns}]

    ap = pd.as_prefixes()
    out = []

    if hasattr(ap, "items"):
        out = [{"prefix": str(k), "uri": str(v)} for k, v in ap.items()]
    else:
        for item in ap:
            if isinstance(item, tuple) and len(item) == 2:
                k, v = item
                out.append({"prefix": str(k), "uri": str(v)})
#                log.debug("      %s %s", k, v)
                continue

            p = (
                getattr(item, "prefixName", None)
                or getattr(item, "prefix", None)
                or getattr(item, "name", None)
            )
            iri = (
                getattr(item, "fullIRI", None)
                or getattr(item, "iri", None)
                or getattr(item, "iriRef", None)
            )
            if p is not None and iri is not None:
                out.append({"prefix": str(p), "uri": str(iri)})

    if not any(d["uri"] == ns for d in out):
        out.append({"prefix": "", "uri": ns})

#    log.debug("Prefixes extracted: %s", out)
    return out

def get_definition(g: Graph, s: URIRef) -> str:
    return get_first_literal(g, s, [SKOS.definition]) or get_first_literal(g, s, [DCTERMS.description]) or ""

def get_qname(uri, ns: str, prefix_map: dict):
    if uri is None or not str(uri).strip():
        log.error("Invalid URI provided to get_qname: %s", uri)
        return "INVALID_URI"
    s = str(uri)
    norm = _norm_base(s)
    # log.debug("Processing URI: %s, normalized: %s, namespace: %s", s, norm, ns)

    # 1. Local namespace (no prefix)
    if norm == _norm_base(ns) or s.startswith(ns):
        local = s[len(_norm_base(ns)):]
        if local.startswith(('/', '#', '_')):
            local = local[1:]
        qname = local.rstrip()
        # log.debug("Matched default namespace, returning QName: %s", qname)
        return qname

    # 2. External prefixes (match longest URI first)
    if not prefix_map:
        log.warning("Empty prefix map for URI: %s, namespace: %s", s, ns)
        return s

    for prefix, uri_base in sorted(prefix_map.items(), key=lambda x: len(x[1]), reverse=True):
        uri_norm = _norm_base(uri_base)
        # log.debug("Checking prefix %s → %s", prefix, uri_base)
        if s == uri_base or s.startswith(uri_base) or norm == uri_norm or norm.startswith(uri_norm):
            local = s[len(uri_base):]
            if local.startswith(('/', '#', '_')):
                local = local[1:]
            local = local.rstrip()
            if not local:
                local = s
            qname = local if prefix == "" else f"{prefix}:{local}"
            return qname

    # Fallback
    if not s.startswith('N'):
        log.warning("No prefix found for URI: %s, namespace: %s, prefix_map:", s, ns)
        for p, u in prefix_map.items():
            log.debug("  %s → %s", p, u)
    return s

def get_label(g: Graph, c: URIRef) -> str:
    if c is None:
        log.error("Invalid class URI provided to get_label: None")
        return "INVALID_CLASS"
#    for _, _, lbl in g.triples((c, RDFS.label, None)):
#        if isinstance(lbl, Literal):
#            return str(lbl)
    fragment_start = max(c.rfind('#'), c.rfind('/')) + 1
    return c[fragment_start:]

def get_first_literal(g: Graph, subj: URIRef, preds: Iterable[URIRef]) -> Optional[str]:
    if subj is None:
        log.error("Invalid subject URI provided to get_first_literal: None")
        return None
    for p in preds:
        for _, _, lit in g.triples((subj, p, None)):
            if isinstance(lit, Literal):
                return str(lit)
    return None

def get_ontology_metadata(g: Graph, ns: str, predicate: URIRef) -> Optional[str]:
    """Extract metadata (e.g., dc:title, dcterms:description) from any subject in the graph."""
    for subj in g.subjects(predicate=predicate):
        for _, _, lit in g.triples((subj, predicate, None)):
            if isinstance(lit, Literal):
                return str(lit)
    ontology_iri = URIRef(ns.rstrip('#/'))
    for _, _, lit in g.triples((ontology_iri, predicate, None)):
        if isinstance(lit, Literal):
            return str(lit)
    return None

def get_preferred_prefix(g: Graph) -> str | None:
    """Extract vann:preferredNamespacePrefix from the owl:Ontology node."""
    
    for ont in g.subjects(RDF.type, OWL.Ontology):
        for pref in g.objects(ont, VANN.preferredNamespacePrefix):
            return str(pref).strip()
    return ""

def iter_annotations(g: Graph, subj: URIRef, ns: str, prefix_map: dict) -> Iterable[Tuple[str, str]]:
    """Yield (predicate_qname, literal) for annotations on subj, excluding DESC_PROPS and SKIP_IN_OTHER."""
    for p, o in sorted(g.predicate_objects(subj), key=lambda po: get_qname(po[0], ns, prefix_map).lower()):
        if isinstance(o, Literal) and p not in SKIP_IN_OTHER:
            yield get_qname(p, ns, prefix_map), str(o)

def collect_list(g: Graph, node) -> list:
    """Collect RDF list members into a Python list."""
    members = []
    while node != RDF.nil:
        first = g.value(node, RDF.first)
        if first:
            members.append(first)
        node = g.value(node, RDF.rest)
    return members

def get_class_expression_str(g: Graph, expr, ns: str, prefix_map: dict) -> str:
    """Convert complex class expression to string representation."""
    if isinstance(expr, URIRef):
        return get_qname(expr, ns, prefix_map)
    if isinstance(expr, BNode):
        union_col = g.value(expr, OWL.unionOf)
        if union_col and union_col != RDF.nil:
            members = collect_list(g, union_col)
            return " or ".join(sorted(get_class_expression_str(g, m, ns, prefix_map) for m in members))
        inter_col = g.value(expr, OWL.intersectionOf)
        if inter_col and inter_col != RDF.nil:
            members = collect_list(g, inter_col)
            return " and ".join(sorted(get_class_expression_str(g, m, ns, prefix_map) for m in members))
        complement = g.value(expr, OWL.complementOf)
        if complement:
            return "not " + get_class_expression_str(g, complement, ns, prefix_map)
        oneOf_members = collect_oneOf(g, expr)
        if oneOf_members:
            return "Enum: " + ", ".join(sorted(get_qname(m, ns, prefix_map) for m in oneOf_members))
        return "ComplexExpr"  # Fallback
    return str(expr)

def get_hyperlinked_class_expression(
    g: Graph,
    expr,
    ns: str,
    prefix_map: dict,
    global_all_classes: set,
    current_doc_dir: str = ".",
) -> str:
    """Convert complex class expression to hyperlinked markdown string."""
    if isinstance(expr, URIRef):
        qname = get_qname(expr, ns, prefix_map)
        return hyperlink_concept(expr, ns, prefix_map, global_all_classes, qname, current_doc_dir=current_doc_dir)
    else:  # BNode
        union_col = g.value(expr, OWL.unionOf)
        if union_col and union_col != RDF.nil:
            members = collect_list(g, union_col)
            return " or ".join(sorted(get_hyperlinked_class_expression(g, m, ns, prefix_map, global_all_classes, current_doc_dir=current_doc_dir) for m in members))
        inter_col = g.value(expr, OWL.intersectionOf)
        if inter_col and inter_col != RDF.nil:
            members = collect_list(g, inter_col)
            return " and ".join(sorted(get_hyperlinked_class_expression(g, m, ns, prefix_map, global_all_classes, current_doc_dir=current_doc_dir) for m in members))
        complement = g.value(expr, OWL.complementOf)
        if complement:
            return "not " + get_hyperlinked_class_expression(g, complement, ns, prefix_map, global_all_classes, current_doc_dir=current_doc_dir)
        oneOf_members = collect_oneOf(g, expr)
        if oneOf_members:
            return "Enum: " + ", ".join(
                sorted(
                    hyperlink_concept(
                        m,
                        ns,
                        prefix_map,
                        global_all_classes,
                        get_qname(m, ns, prefix_map),
                        current_doc_dir=current_doc_dir,
                    )
                    for m in oneOf_members
                )
            )
        # Fallback
        return get_class_expression_str(g, expr, ns, prefix_map)

def get_leaf_classes(g: Graph, expr, ns: str, prefix_map: dict) -> list:
    """Recursively get leaf classes from class expressions."""
    leaves = []
    if isinstance(expr, URIRef):
        leaves.append(expr)
    elif isinstance(expr, BNode):
        union_col = g.value(expr, OWL.unionOf)
        inter_col = g.value(expr, OWL.intersectionOf)
        complement = g.value(expr, OWL.complementOf)
        oneOf_members = collect_oneOf(g, expr)
        if union_col and union_col != RDF.nil:
            members = collect_list(g, union_col)
            for m in members:
                leaves.extend(get_leaf_classes(g, m, ns, prefix_map))
        elif inter_col and inter_col != RDF.nil:
            members = collect_list(g, inter_col)
            for m in members:
                leaves.extend(get_leaf_classes(g, m, ns, prefix_map))
        elif complement:
            leaves.extend(get_leaf_classes(g, complement, ns, prefix_map))
        elif oneOf_members:
            # Changed based on Copilot suggestion to treat oneOf members as leaves, since they represent individuals in an enumeration
            #            leaves.extend(oneOf_members)  # Treat individuals as leaves
            for m in oneOf_members:
                leaves.extend(get_leaf_classes(g, m, ns, prefix_map))
        else:
            leaves.append(expr)  # Fallback for other BNodes
    return leaves

def collect_oneOf(g: Graph, node) -> list:
    """Collect members of owl:oneOf if present."""
    if (node, RDF.type, OWL.Class) in g:
        oneOf_col = g.value(node, OWL.oneOf)
        if oneOf_col and oneOf_col != RDF.nil:
            return collect_list(g, oneOf_col)
    return []

def class_restrictions(
    g: Graph,
    cls: URIRef,
    ns: str,
    prefix_map: dict,
    global_all_classes: set,
    current_doc_dir: str = ".",
) -> List[Tuple[str, str]]:
    rows = []
    for restr in g.objects(cls, RDFS.subClassOf):
        if (restr, RDF.type, OWL.Restriction) in g:
            prop = g.value(restr, OWL.onProperty)
            if prop:
                prop_qname = get_qname(prop, ns, prefix_map)
                hyper_prop = hyperlink_concept(prop, ns, prefix_map, global_all_classes, prop_qname, current_doc_dir=current_doc_dir)
                constr_parts = []
                # Cardinality
                for card_p, card_label in [(OWL.cardinality, 'exactly'), (OWL.minCardinality, 'min'), (OWL.maxCardinality, 'max')]:
                    card = g.value(restr, card_p)
                    if card:
                        constr_parts.append(f"{card_label} {card}")
                # Qualified cardinality
                for qcard_p, qcard_label in [(OWL.qualifiedCardinality, 'exactly'), (OWL.minQualifiedCardinality, 'min'), (OWL.maxQualifiedCardinality, 'max')]:
                    qcard = g.value(restr, qcard_p)
                    if qcard:
                        constr_parts.append(f"{qcard_label} {qcard}")
                # Values from
                for values_p, values_label in [(OWL.allValuesFrom, 'only'), (OWL.someValuesFrom, 'some')]:
                    values = g.value(restr, values_p)
                    if values:
                        values_str = get_hyperlinked_class_expression(g, values, ns, prefix_map, global_all_classes, current_doc_dir=current_doc_dir)
                        constr_parts.append(f"{values_label} {values_str}")
                # hasValue
                has_value = g.value(restr, OWL.hasValue)
                if has_value:
                    if isinstance(has_value, Literal):
                        constr_parts.append(f"value '{has_value}'")
                    else:
                        hv_qname = get_qname(has_value, ns, prefix_map)
                        hyper_hv = hyperlink_concept(has_value, ns, prefix_map, global_all_classes, hv_qname, current_doc_dir=current_doc_dir)
                        constr_parts.append(f"value {hyper_hv}")
                if constr_parts:
                    rows.append((hyper_prop, ' '.join(constr_parts)))
    return rows

def fmt_title(name: str, all_classes: set, ns: str, abstract_map: dict) -> str:
    """Format class title for Graphviz, with URL attribute for local classes."""
    display_name = f"<I>{insert_spaces(name)}</I>" if abstract_map.get(name, False) else insert_spaces(name)
    return display_name

def is_abstract(cls, g, ns):
    if cls is None:
        log.error("Invalid class URI provided to is_abstract: None")
        return False
    abstract = g.value(cls, URIRef(ns + "abstract"))
    if abstract is None:
        abstract = g.value(cls, URIRef("http://protege.stanford.edu/ontologies/metadata#abstract"))
    return abstract is not None and str(abstract).lower() == "true"

def get_id(qname):
    if not qname:
        log.error("Invalid qname provided to get_id: %s", qname)
        return "INVALID_QNAME"
    if ':' in qname:
        prefix, local = qname.split(':', 1)
        return prefix + '_' + local
    return qname

def get_all_class_superclasses(cls, g):
    if cls is None:
        log.error("Invalid class URI provided to get_all_class_superclasses: None")
        return set()
    direct_supers = set()
    for super_cls in g.objects(cls, RDFS.subClassOf):
        if isinstance(super_cls, URIRef) and super_cls != OWL.Thing and (super_cls, RDF.type, OWL.Class) in g:
            direct_supers.add(super_cls)
    all_supers = set(direct_supers)
    for sup in direct_supers:
        all_supers.update(get_all_class_superclasses(sup, g))
    return all_supers

def get_property_info(g: Graph, prop: URIRef, ns: str, prefix_map: dict) -> tuple:
    """Get property name, handling inverses."""
    if not prop:
        return None, False, None
    inverse_of = g.value(prop, OWL.inverseOf)
    if inverse_of:
        base_prop = inverse_of
        is_inverse = True
        prop_name = f"inverse {get_qname(base_prop, ns, prefix_map)}"
    else:
        base_prop = prop
        is_inverse = False
        prop_name = get_qname(base_prop, ns, prefix_map)
    return prop_name, is_inverse, base_prop

def is_refined_property(g: Graph, cls: URIRef, prop: URIRef, restriction: URIRef) -> bool:
    """Check if a property restriction in cls refines an inherited restriction."""
    if cls is None or prop is None or restriction is None:
        log.error("Invalid input to is_refined_property: cls=%s, prop=%s, restriction=%s", cls, prop, restriction)
        return False
    all_supers = get_all_class_superclasses(cls, g)
    for super_cls in all_supers:
        for super_restr in g.objects(super_cls, RDFS.subClassOf):
            if (super_restr, RDF.type, OWL.Restriction) in g:
                super_prop = g.value(super_restr, OWL.onProperty)
                if super_prop == prop:
                    current_avf = g.value(restriction, OWL.allValuesFrom)
                    super_avf = g.value(super_restr, OWL.allValuesFrom)
                    current_card = g.value(restriction, OWL.qualifiedCardinality) or g.value(restriction, OWL.minQualifiedCardinality) or g.value(restriction, OWL.maxQualifiedCardinality)
                    super_card = g.value(super_restr, OWL.qualifiedCardinality) or g.value(super_restr, OWL.minQualifiedCardinality) or g.value(super_restr, OWL.maxQualifiedCardinality)
                    current_on_class = g.value(restriction, OWL.onClass)
                    super_on_class = g.value(super_restr, OWL.onClass)
                    if (current_avf != super_avf or
                        current_card != super_card or
                        current_on_class != super_on_class):
                        return True
    return False

import re

def prefix_to_uml_namespace(qname: str) -> str:
    """Convert a QName prefix to a UML-friendly namespace (e.g., 'time:date' → 'time::date')."""
    if ':' in qname:
        prefix, local = qname.split(':', 1)
        return f"{prefix}::{local}"
    return qname

def insert_spaces(name: str) -> str:
    """Convert camelCase or hyphenated names to readable Title Case with spaces.
    
    Examples:
        fuzzy-time          → Fuzzy Time
        fuzzy-time-pattern  → Fuzzy Time Pattern
        LandUsePattern      → Land Use Pattern
        ClockTime           → Clock Time
        ITSThing            → ITS Thing
    """
    if not name:
        log.error("Invalid name provided to insert_spaces: %s", name)
        return "INVALID_NAME"

    # First, replace hyphens with spaces
    name = name.replace('-', ' ')

    # Then handle camelCase (insert space before uppercase letters)
    name = re.sub(r'([a-z\d])([A-Z])', r'\1 \2', name)
    name = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', name)

    # Convert to Title Case (each word capitalized)
    name = ' '.join(word.capitalize() for word in name.split())

    return name

def get_ontology_for_uri(uri_str: str, ns_to_ontology: dict) -> str:
    norm_uri = _norm_base(uri_str)
    for ont_ns, ont_name in sorted(ns_to_ontology.items(), key=lambda x: len(x[0]), reverse=True):
        if norm_uri.startswith(_norm_base(ont_ns)):
            return ont_name
    return None

def hyperlink_concept(
    uri_or_qname,
    ns: str,
    prefix_map: dict,
    global_all_classes: set,
    qname: str = None,
    current_doc_dir: str = ".",
) -> str:
    """Create markdown hyperlink for classes or properties.

    current_doc_dir:
      - "index" for `docs/index.md`
      - "." for root-level docs pages (e.g., class pages like `docs/Foo.md`)
      - "properties" for docs under `docs/properties/` (e.g., property pages)
    """
    if isinstance(uri_or_qname, str):
        # Try to resolve qname to full URI
        uri = None
        if ':' not in uri_or_qname:
            uri_or_qname = f"{ns}{uri_or_qname}"
        prefix, local = uri_or_qname.split(':', 1)
        for p, base in prefix_map.items():
            if p == prefix or (p == "" and prefix == ""):
                uri = URIRef(f"{base}{local}")
                break
        if uri is None:
            uri = URIRef(uri_or_qname)  # fallback
    else:
        uri = uri_or_qname

    if not qname:
        qname = get_qname(uri, ns, prefix_map)

    iri = str(uri)

    def _prefix_to_site_root() -> str:
        cd = current_doc_dir.strip("/").lower()
        if cd in ("", ".", "index"):
            return ""
        if cd == "classes":
            return ""
        if cd == "properties":
            return "../"
        return ""

    # Local class → markdown link (MkDocs rewrites to site URL)
    if qname in global_all_classes:
        prefix = _prefix_to_site_root()
        if current_doc_dir.strip("/").lower() == "properties":
            return f"[{qname}](../classes/{qname}.md)"
        if current_doc_dir.strip("/").lower() == "classes":
            return f"[{qname}]({qname}.md)"
        return f"[{qname}](classes/{qname}.md)"

    # Local property (new)
    if iri.startswith(ns):
        prefix = _prefix_to_site_root()
        if current_doc_dir.strip("/").lower() == "classes":
            return f"[{qname}](../properties/{qname}.md)"
        return f"[{qname}]({prefix}properties/{qname}.md)"

    # External known ontologies
    if iri.startswith("https://w3id.org/citydata/") or iri.startswith("https://w3id.org/itsdata/"):
        return f"[{qname}]({iri})"

    # Other external
    prefix, local = qname.split(':', 1) if ':' in qname else ('', qname)

    return f"[{qname}](https://w3id.org/citydata/imported/{prefix}/{local})"

def get_url(uri: URIRef, ns: str, prefix_map: dict, global_all_classes: set, withMd: bool = True) -> str:
    if not uri:
        return None
    iri = str(uri)
    qname = get_qname(uri, ns, prefix_map)
    if iri.startswith(ns):
        return f"{qname}.md" if withMd else f"../{qname}"
    elif iri.startswith("https://w3id.org/citydata/") or iri.startswith("https://w3id.org/itsdata/"):
        return iri
    else:
        prefix, local = qname.split(':', 1) if ':' in qname else ('', qname)
        return f"https://w3id.org/citydata/imported/{prefix}/latest/{local}"

def parse_concept_registry(script_dir):
    """Same as in your owl version – kept for consistency."""
    registry_path = os.path.join(script_dir, "concept_registry.md")
    if not os.path.exists(registry_path):
        with open(registry_path, 'w', encoding='utf-8') as f:
            f.write("| base_uri | name | type | description |\n|----------|------|------|-------------|\n")
        log.info(f"Created new concept_registry.md in {script_dir}")
        return {}
    content = open(registry_path, 'r', encoding='utf-8').read()
    lines = content.splitlines()
    registry = {}
    in_table = False
    headers = None
    for line in lines:
        if line.strip().startswith('|'):
            if not in_table:
                headers = [h.strip().lower() for h in line.split('|') if h.strip()]
                log.debug(f"Parsed headers: {headers}")
                in_table = True
            elif headers and not line.strip().startswith('|---'):
                values = [v.strip() for v in line.split('|') if v.strip()]
                if len(values) < 3:
                    continue
                try:
                    base_uri = values[headers.index('base_uri')]
                    name = values[headers.index('name')]
                    concept_type = values[headers.index('type')]
                    description = values[headers.index('description')] if 'description' in headers and len(values) > headers.index('description') else ''
                    uri = f"{base_uri}{name}"
                    registry[uri] = {'type': concept_type, 'description': description}
                except Exception as e:
                    log.warning(f"Skipping row: {line} ({str(e)})")
    log.debug(f"Loaded {len(registry)} entries from concept_registry.md")
    return registry

def update_concept_registry(script_dir, registry):
    registry_path = os.path.join(script_dir, "concept_registry.md")
    with open(registry_path, 'w', encoding='utf-8') as f:
        f.write("![Draft for review only](../../assets/img/draft_for_review.svg)\n\n")
        f.write("# Concept Registry\n\n")
        f.write("This page lists all known concepts (classes and properties) included in the RITSO.\n\n")
        f.write("| base_uri | name | type | description |\n|----------|------|------|-------------|\n")
        # Sort by base_uri and then name
        sorted_items = sorted(registry.items(), key=lambda x: (x[0].rsplit('/', 1)[0] if '/' in x[0] else x[0], x[0].rsplit('/', 1)[1] if '/' in x[0] else ''))
        for uri, info in sorted_items:
            base_uri, name = uri.rsplit('/', 1) if '/' in uri else (uri, '')
            if '#' in name:
                base_uri, name = f"{base_uri}/{name.split('#')[0]}#", name.split('#')[1]
            if not base_uri.endswith(('#', '/')):
                base_uri += '/'
            if not base_uri.startswith('N'):
                f.write(f"| `{base_uri}` | {name} | {info['type']} | {info['description']} |\n")
    log.info(f"Updated concept_registry.md with {len(registry)} entries")

def parse_ontology_registry(script_dir):
    registry_path = os.path.join(script_dir, "ontology_registry.md")
    if not os.path.exists(registry_path):
        with open(registry_path, 'w', encoding='utf-8') as f:
            f.write("|Prefix | Official IRI                                       | RITSO Location     | Description |\n|----------|------|------|-------------|\n")
        log.info(f"Created new ontology_registry.md in {script_dir}")
        return {}
    content = open(registry_path, 'r', encoding='utf-8').read()
    lines = content.splitlines()
    registry = {}
    in_table = False
    headers = None
    for line in lines:
        if line.strip().startswith('|'):
            if not in_table:
                headers = [h.strip().lower() for h in line.split('|') if h.strip()]
                log.debug(f"Parsed headers: {headers}")
                in_table = True
            elif headers and not line.strip().startswith('|---'):
                values = [v.strip() for v in line.split('|') if v.strip()]
                log.debug(f"Parsed values: {values}")
                if len(values) < 4:  # Require all four columns
                    log.warning(f"Skipping row with insufficient values (expected 4, got {len(values)}): {line}")
                    continue
                try:
                    preferred_prefix = values[0]
                    official_iri = values[1]
                    ritso_location = values[2]
                    description = values[3]
                    registry[official_iri] = {'preferred_prefix': preferred_prefix, 'ritso_location': ritso_location, 'description': description}
                except ValueError as e:
                    log.warning(f"Skipping row due to missing header: {line} ({str(e)})")
    log.debug(f"Loaded {len(registry)} entries from ontology_registry.md")
    return registry

def update_ontology_registry(script_dir, ontology_registry):
    registry_path = os.path.join(script_dir, "ontology_registry.md")
    with open(registry_path, 'w', encoding='utf-8') as f:
        f.write("![Draft for review only](/assets/img/draft_for_review.svg)\n\n")
        f.write("# Ontology Registry\n\n")
        f.write("This page lists all known ontologies included in the RITSO.\n\n")
        f.write("|Prefix | Official IRI | RITSO Location | Description |\n|-------|--------------|----------------|-------------|\n")
        # Sort by preferred_prefix (case-insensitive)
        sorted_items = sorted(ontology_registry.items(), key=lambda x: x[1]['preferred_prefix'].lower())
        for iri, info in sorted_items:
            f.write(f"| {info['preferred_prefix']} | {iri} | {info['ritso_location']} | {info['description']} |\n")
    log.info(f"Updated ontology_registry.md with {len(ontology_registry)} entries")

def get_shacl_constraints(g: Graph, cls: URIRef, ns: str, prefix_map: dict) -> dict:
    """
    Extract human-readable SHACL constraints for properties of a target class.
    
    Correctly combines cardinality (from property shape or node shape)
    with sh:class (which can be on either).
    
    For the pattern:
        sh:property [
            sh:path :timeReference ;
            sh:node its-sh:ExactlyOneShape ;   # provides "exactly 1"
            sh:class :FuzzyTimeCode ;          # "only FuzzyTimeCode"
        ]
    
    This now correctly produces:  "exactly 1 FuzzyTimeCode"
    """
    constraints = defaultdict(list)

    def _get_cardinality(shape: URIRef) -> tuple:
        """Return (minc, maxc) if present on this shape."""
        minc = g.value(shape, SH.minCount)
        maxc = g.value(shape, SH.maxCount)
        return (minc, maxc) if minc is not None or maxc is not None else (None, None)

    def _get_class(shape: URIRef) -> URIRef | None:
        return g.value(shape, SH['class'])

    def _get_datatype(shape: URIRef) -> URIRef | None:
        return g.value(shape, SH.datatype)

    for shape in g.subjects(SH.targetClass, cls):
        for prop_shape in g.objects(shape, SH.property):
            path = g.value(prop_shape, SH.path)
            if not path:
                continue

            prop_name = get_qname(path, ns, prefix_map)

            # Gather cardinality and class from all relevant shapes
            min_count = None
            max_count = None
            class_uris = []
            datatype = None
            unique_lang = False
            node_refs = list(g.objects(prop_shape, SH.node))
            shapes_to_check = [prop_shape] + node_refs

            for s in shapes_to_check:
                # Cardinality
                mc = g.value(s, SH.minCount)
                Mc = g.value(s, SH.maxCount)
                if mc is not None:
                    min_count = mc
                if Mc is not None:
                    max_count = Mc

                # sh:class
                cl = g.value(s, SH['class'])
                if cl and cl not in class_uris:
                    class_uris.append(cl)

                # datatype (rarely combined with class, but included)
                dt = g.value(s, SH.datatype)
                if dt:
                    datatype = dt

                if g.value(s, SH.uniqueLang) is not None:
                    unique_lang = True

            # Fallback when reusable multilingual shapes are referenced but not loaded
            for node_ref in node_refs:
                if not isinstance(node_ref, URIRef):
                    continue
                local = str(node_ref).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
                if "Multilingual" not in local:
                    continue
                unique_lang = True
                if datatype is None:
                    datatype = URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#langString")
                if min_count is None and max_count is None:
                    if local.startswith("ExactlyOne") or local.startswith("MinOne"):
                        min_count = Literal(1)

            # Now build the readable constraint strings
            if datatype:
                dt_name = get_qname(datatype, ns, prefix_map)
                if min_count is not None and max_count is not None and min_count == max_count:
                    constraints[prop_name].append(f"exactly {min_count} {dt_name}")
                elif min_count is not None or max_count is not None:
                    if min_count is not None:
                        constraints[prop_name].append(f"min {min_count} {dt_name}")
                    if max_count is not None:
                        constraints[prop_name].append(f"max {max_count} {dt_name}")
                elif unique_lang:
                    # Multilingual shapes without min/max → one value per language
                    constraints[prop_name].append(f"0..* {dt_name} (one per language)")
                else:
                    # no cardinality, just datatype
                    constraints[prop_name].append(f"datatype {dt_name}")

            elif class_uris:
                # Build class names with hyperlinks
                class_links = []
                for c in class_uris:
                    qname = get_qname(c, ns, prefix_map)          # e.g. "its:FuzzyTimeCode"
                    iri = str(c)                                  # full IRI as string
                    
                    # Markdown hyperlink (most common for docs)
                    link = f"[{qname}]({iri})"
                    class_links.append(link)
                
                class_str = " or ".join(class_links) if len(class_links) > 1 else class_links[0]

                if min_count is not None and max_count is not None:
                    if min_count == max_count:
                        constraints[prop_name].append(f"exactly {min_count} {class_str}")
                    else:
                        constraints[prop_name].append(f"min {min_count} and max {max_count} {class_str}")
                elif min_count is not None or max_count is not None:
                    if min_count is not None:
                        constraints[prop_name].append(f"min {min_count} {class_str}")
                    if max_count is not None:
                        constraints[prop_name].append(f"max {max_count} {class_str}")
                else:
                    constraints[prop_name].append(f"only {class_str}")

            else:
                # No class and no datatype → plain cardinality only
                if min_count is not None and max_count is not None and min_count == max_count:
                    constraints[prop_name].append(f"exactly {min_count}")
                else:
                    if min_count is not None:
                        constraints[prop_name].append(f"min {min_count}")
                    if max_count is not None:
                        constraints[prop_name].append(f"max {max_count}")
    return constraints

def get_shacl_diagram_constraints(g: Graph, cls: URIRef, ns: str, prefix_map: dict) -> dict:
    """Extract property constraints from SHACL.
    
    Returns {prop_qname: {'multiplicity': '0..1' or '1' or '1..*', ... , 'range': 'xsd:string' or 'TargetClass'}}
    """
    constraints = defaultdict(dict)

    def _local_name(uri) -> str:
        s = str(uri)
        return s.rsplit("/", 1)[-1].rsplit("#", 1)[-1]

    def _apply_node_ref_conventions(node_uri) -> None:
        """When CoreSHACL is not loaded, sh:node still names the reusable shape.

        Infer multiplicity / datatype / uniqueLang from the IRI local name
        (e.g. ExactlyOneShape, MaxOneMultilingualShape).
        """
        local = _local_name(node_uri)
        if not local.endswith("Shape"):
            return

        # Cardinality from standard reusable shapes
        if "multiplicity" not in constraints[prop_name]:
            if local.startswith("ExactlyOne"):
                # Multilingual "exactly one" means ≥1 with one-per-language
                constraints[prop_name]["multiplicity"] = (
                    "1..*" if "Multilingual" in local else "1"
                )
            elif local.startswith("MaxOne"):
                constraints[prop_name]["multiplicity"] = (
                    "0..*" if "Multilingual" in local else "0..1"
                )
            elif local.startswith("MinOne"):
                constraints[prop_name]["multiplicity"] = "1..*"

        if "Multilingual" in local:
            constraints[prop_name]["uniqueLang"] = True
            constraints[prop_name].setdefault("datatype", "rdf:langString")

    def _get_multiplicity(shape) -> str | None:
        minc = g.value(shape, SH.minCount)
        maxc = g.value(shape, SH.maxCount)

        if minc is not None or maxc is not None:
            # Normalize to UML-style string
            if minc is not None and maxc is not None and minc == maxc:
                if minc == 1:
                    return "1"          # most common shorthand
                return f"{minc}"        # exactly N → just "N" or "3"

            min_str = str(minc) if minc is not None else "0"
            max_str = str(maxc) if maxc is not None else "*"
            return f"{min_str}..{max_str}"

        # Multilingual reusable shapes use sh:uniqueLang without min/max
        # (one value per language). Treat as open cardinality for UML.
        if g.value(shape, SH.uniqueLang) is not None:
            return "0..*"

        return None

    for shape in g.subjects(SH.targetClass, cls):
        for prop_shape in g.objects(shape, SH.property):
            path = g.value(prop_shape, SH.path)
            if not path:
                continue
            prop_name = get_qname(path, ns, prefix_map)

            node_refs = list(g.objects(prop_shape, SH.node))

            # Direct constraints + sh:node reusable shapes
            for s in [prop_shape] + node_refs:
                mult = _get_multiplicity(s)
                if mult:
                    constraints[prop_name]['multiplicity'] = mult

                # Range / class (for both data and object)
                sh_class = g.value(s, SH['class'])
                if sh_class:
                    constraints[prop_name]['range'] = get_qname(sh_class, ns, prefix_map)

                # Datatype
                sh_datatype = g.value(s, SH.datatype)
                if sh_datatype:
                    constraints[prop_name]['datatype'] = get_qname(sh_datatype, ns, prefix_map)

                # Multilingual: at most one literal per language tag
                if g.value(s, SH.uniqueLang) is not None:
                    constraints[prop_name]['uniqueLang'] = True

            # Fallback when reusable shapes are referenced but not loaded
            for node_ref in node_refs:
                if isinstance(node_ref, URIRef):
                    _apply_node_ref_conventions(node_ref)

    return constraints

def get_reqview_id(g: Graph, concept: URIRef, ns: str, prefix_map: dict) -> str:
    """Extract ReqView object ID from custom annotation.
    Looks for its-core:reqviewId or similar property."""
    # Adjust the property URI to match your ontology
    REQVIEW_ID_PROP = URIRef("https://w3id.org/itsdata/core/v1/reqviewId")
    
    rid = g.value(concept, REQVIEW_ID_PROP)
    if rid and isinstance(rid, Literal):
        return str(rid)
    return ""  # no ID yet