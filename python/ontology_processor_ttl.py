# ontology_processor_ttl.py
import io
import os
import logging
import re
import ssl
import time
import urllib.error
import urllib.request
from rdflib import Graph, RDF, OWL, RDFS, URIRef
from rdflib.namespace import SH, VANN

from utils import (
    get_qname,
    discover_oasis_catalogs,
    resolve_import_iri,
)

log = logging.getLogger("ttl2mkdocs")

_RDF_ACCEPT = (
    "text/turtle, application/rdf+xml, application/owl+xml, "
    "application/ld+json, text/n3, application/n-triples, */*;q=0.1"
)


def _content_type_to_format(content_type: str | None) -> str | None:
    if not content_type:
        return None
    ct = content_type.split(";", 1)[0].strip().lower()
    return {
        "text/turtle": "turtle",
        "application/x-turtle": "turtle",
        "application/turtle": "turtle",
        "application/rdf+xml": "xml",
        "application/owl+xml": "xml",
        "application/xml": "xml",
        "text/xml": "xml",
        "text/n3": "n3",
        "application/n-triples": "nt",
        "application/ld+json": "json-ld",
    }.get(ct)


def _fetch_url_bytes(url: str, retries: int = 4, timeout: float = 45.0) -> tuple[bytes, str | None]:
    """
    Download a remote ontology with retries.

    RDFLib's default urlopen is a single attempt; w3id.org redirect chains often
    fail transiently with SSL UNEXPECTED_EOF_WHILE_READING. Fetching ourselves
    lets us retry and then parse from memory.
    """
    headers = {
        "User-Agent": "ont2md-ttl2md/1.0 (+https://github.com/; ontology documentation)",
        "Accept": _RDF_ACCEPT,
        "Connection": "close",
    }
    ctx = ssl.create_default_context()
    last_err: Exception | None = None

    # w3id and some hosts treat trailing slash as significant; try both if needed
    candidates = [url]
    if url.endswith("/"):
        candidates.append(url.rstrip("/"))
    else:
        candidates.append(url + "/")

    for candidate in candidates:
        for attempt in range(1, retries + 1):
            try:
                req = urllib.request.Request(candidate, headers=headers, method="GET")
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                    data = resp.read()
                    ctype = resp.headers.get("Content-Type")
                    if not data:
                        raise ValueError(f"Empty response from {candidate}")
                    log.debug(
                        "Fetched %s (%d bytes, Content-Type=%s) on attempt %d",
                        candidate,
                        len(data),
                        ctype,
                        attempt,
                    )
                    return data, ctype
            except (urllib.error.URLError, ssl.SSLError, TimeoutError, ConnectionError, ValueError) as e:
                last_err = e
                log.warning(
                    "Fetch attempt %d/%d failed for %s: %s",
                    attempt,
                    retries,
                    candidate,
                    e,
                )
                if attempt < retries:
                    time.sleep(min(2.0, 0.4 * attempt))
        # exhausted retries for this candidate; try alternate slash form
    raise last_err or urllib.error.URLError(f"Failed to fetch {url}")


def _parse_rdf_bytes(g: Graph, data: bytes, content_type: str | None = None, public_id: str | None = None) -> None:
    """Parse RDF bytes into g using Content-Type hint, then format fallbacks on a temp graph."""
    source = io.BytesIO(data)
    hinted = _content_type_to_format(content_type)
    formats = []
    if hinted:
        formats.append(hinted)
    for fmt in ("xml", "turtle", "n3", "nt", "json-ld"):
        if fmt not in formats:
            formats.append(fmt)

    last_err = None
    for fmt in formats:
        try:
            tmp = Graph()
            tmp.parse(source, format=fmt, publicID=public_id)
            g += tmp
            return
        except Exception as e:
            last_err = e
            source.seek(0)
    # Last resort: let rdflib guess without an explicit format
    try:
        source.seek(0)
        g.parse(source, publicID=public_id)
        return
    except Exception as e:
        last_err = e
    raise last_err or ValueError("Could not parse RDF bytes")


def parse_concept_registry(script_dir):
    """Same as before – kept for consistency."""
    registry_path = os.path.join(script_dir, "concept_registry.md")
    if not os.path.exists(registry_path):
        with open(registry_path, "w", encoding="utf-8") as f:
            f.write(
                "| base_uri | name | type | description |\n"
                "|----------|------|------|-------------|\n"
            )
        log.info(f"Created new concept_registry.md in {script_dir}")
        return {}
    content = open(registry_path, "r", encoding="utf-8").read()
    lines = content.splitlines()
    registry = {}
    in_table = False
    headers = None
    for line in lines:
        if line.strip().startswith("|"):
            if not in_table:
                headers = [h.strip().lower() for h in line.split("|") if h.strip()]
                in_table = True
            elif headers and not line.strip().startswith("|---"):
                values = [v.strip() for v in line.split("|") if v.strip()]
                if len(values) < 3:
                    continue
                try:
                    base_uri = values[headers.index("base_uri")]
                    name = values[headers.index("name")]
                    concept_type = values[headers.index("type")]
                    description = (
                        values[headers.index("description")]
                        if "description" in headers and len(values) > headers.index("description")
                        else ""
                    )
                    uri = f"{base_uri}{name}"
                    registry[uri] = {"type": concept_type, "description": description}
                except Exception:
                    pass
    log.debug(f"Loaded {len(registry)} entries from concept_registry.md")
    return registry


def _extract_master_namespace(ttl_files: list) -> str:
    """Find the true master base namespace from BASE declaration or vann:preferredNamespaceUri."""
    for ttl_path in ttl_files:
        try:
            with open(ttl_path, "r", encoding="utf-8") as f:
                content = f.read()

            base_match = re.search(r"BASE\s+<([^>]+)>", content, re.IGNORECASE)
            if base_match:
                ns = base_match.group(1).rstrip("#/") + "/"
                log.debug(f"Found master namespace from BASE: {ns}")
                return ns

            pref_match = re.search(r"vann:preferredNamespaceUri\s+<([^>]+)>", content)
            if pref_match:
                ns = pref_match.group(1).rstrip("#/") + "/"
                log.info(f"Found master namespace from vann:preferredNamespaceUri: {ns}")
                return ns

        except Exception as e:
            log.warning(f"Could not read {ttl_path} for namespace: {e}")

    log.warning("No BASE or vann:preferredNamespaceUri found – using default")
    return "https://w3id.org/itsdata/time/v1/"


def _parse_import_source(g: Graph, source: str) -> None:
    """Parse a local path or URL into g, trying common RDF serializations.

    Remote ontologies are often RDF/XML (e.g. CityData on w3id.org). Trying Turtle
    first against RDF/XML is unsafe: the Turtle parser can emit many
    "does not look like a valid URI" warnings (XML start-tag text treated as IRIs)
    and may leave bad triples in the graph before failing. Prefer auto-detect /
    Content-Type, and only probe formats on a temporary graph.

    Remote HTTP(S) IRIs are downloaded with retries (w3id redirect/TLS flakes are
    common), then parsed from memory.
    """
    if os.path.isfile(source):
        ext = os.path.splitext(source)[1].lower()
        fmt = {
            ".ttl": "turtle",
            ".turtle": "turtle",
            ".owl": "xml",
            ".rdf": "xml",
            ".xml": "xml",
            ".nt": "nt",
            ".n3": "n3",
            ".jsonld": "json-ld",
        }.get(ext)
        if fmt:
            g.parse(source, format=fmt)
            return
        # Unknown extension: auto-detect without guessing Turtle first
        g.parse(source)
        return

    if source.startswith(("http://", "https://")):
        data, ctype = _fetch_url_bytes(source)
        _parse_rdf_bytes(g, data, content_type=ctype, public_id=source)
        return

    # file: URLs or other opaque sources: let rdflib open them
    try:
        g.parse(source)
        return
    except Exception:
        pass

    last_err = None
    for fmt in ("xml", "turtle", "n3", "nt", "json-ld"):
        try:
            tmp = Graph()
            tmp.parse(source, format=fmt)
            g += tmp
            return
        except Exception as e:
            last_err = e
    raise last_err or ValueError(f"Could not parse import source: {source}")


def _normalize_namespace_uri(uri: str) -> str:
    """Normalize a namespace URI to a consistent trailing form for binding."""
    s = str(uri).strip()
    if not s:
        return s
    if s.endswith(("#", "/")):
        return s
    return s + "/"


def _namespace_from_vann_and_ontology(vann_uri: str, ont: URIRef) -> str:
    """
    Resolve the vocabulary namespace for a preferred prefix.

    Prefer ``vann:preferredNamespaceUri`` when present. If that URI is a parent
    of a versioned ontology IRI (e.g. vann ``.../core/`` with ontology
    ``.../core/v1/``), extend it to include the ``vN/`` segment so qnames do
    not become ``its-core:v1/Code``.

    Do **not** treat pattern/module IRIs (``.../v1/AgreementPattern``) as the
    namespace — those are individuals in the vocabulary, not the NS itself.
    """
    vann = _normalize_namespace_uri(vann_uri) if vann_uri else ""
    ont_s = str(ont).strip()
    if not vann:
        # No vann URI: only accept an ontology IRI that itself looks like a NS.
        if ont_s.endswith(("/", "#")) and re.search(r"/v\d+/$", ont_s):
            return ont_s
        return ""

    if ont_s.startswith(vann):
        rest = ont_s[len(vann) :]
        m = re.match(r"(v\d+)/?", rest)
        if m:
            return vann + m.group(1) + "/"
    return vann


def _prefer_longer_namespace(a: str, b: str) -> str:
    """If one namespace URI is a parent of the other, keep the longer (more specific)."""
    if not a:
        return b
    if not b:
        return a
    if a == b:
        return a
    if b.startswith(a) and len(b) > len(a):
        return b
    if a.startswith(b) and len(a) > len(b):
        return a
    return a


def _rebind_preferred_prefixes(g: Graph, master_ns: str | None = None) -> None:
    """
    Restore curated namespace prefixes after owl:imports.

    Imported Turtle modules often bind their vocabulary to the empty prefix
    (``PREFIX : <.../>``). RDFLib's default bind semantics then *replace* any
    earlier meaningful prefix (e.g. ``its-time``) for that namespace. Later
    RDF/XML imports invent ``defaultN`` bindings for the orphaned namespaces.

    Re-apply ``vann:preferredNamespacePrefix`` / ``vann:preferredNamespaceUri``
    from every loaded Ontology, and restore the master empty prefix.
    """
    preferred: dict[str, str] = {}  # prefix -> namespace URI
    for ont in g.subjects(RDF.type, OWL.Ontology):
        pref = g.value(ont, VANN.preferredNamespacePrefix)
        uri = g.value(ont, VANN.preferredNamespaceUri)
        if not pref:
            continue
        prefix = str(pref).strip()
        if not prefix or re.fullmatch(r"default\d+", prefix):
            continue

        chosen = _namespace_from_vann_and_ontology(str(uri) if uri else "", ont)
        if not chosen:
            continue
        preferred[prefix] = _prefer_longer_namespace(preferred.get(prefix, ""), chosen)

    # Do not shorten an existing longer *versioned* binding for the same prefix.
    existing_by_prefix = {p: str(u) for p, u in g.namespaces()}
    for prefix, uri in list(preferred.items()):
        cur = existing_by_prefix.get(prefix)
        if not cur:
            continue
        # Only keep existing if it is vann/parent + vN/ (not a pattern path).
        if cur.startswith(uri) and re.match(re.escape(uri) + r"v\d+/$", cur):
            preferred[prefix] = cur

    for prefix, uri in sorted(preferred.items(), key=lambda kv: -len(kv[1])):
        try:
            g.namespace_manager.bind(prefix, URIRef(uri), override=True, replace=True)
            log.debug("Rebound preferred prefix %s: → %s", prefix, uri)
        except Exception as e:
            log.warning("Could not bind preferred prefix %s: → %s: %s", prefix, uri, e)

    if master_ns:
        try:
            g.namespace_manager.bind("", URIRef(master_ns), override=True, replace=True)
        except Exception as e:
            log.warning("Could not restore master empty prefix for %s: %s", master_ns, e)


def _build_prefix_map(g: Graph, master_ns: str) -> dict:
    """
    Build prefix→URI map for qname rendering.

    Prefer real prefixes over RDFLib ``defaultN`` placeholders when both exist.
    Prefer the longest *versioned* namespace URI for each prefix.
    """
    by_prefix: dict[str, str] = {}
    for prefix, uri in g.namespaces():
        u = str(uri)
        if re.fullmatch(r"default\d+", prefix or ""):
            continue
        by_prefix[prefix] = _prefer_longer_namespace(by_prefix.get(prefix, ""), u)

    for ont in g.subjects(RDF.type, OWL.Ontology):
        pref = g.value(ont, VANN.preferredNamespacePrefix)
        uri = g.value(ont, VANN.preferredNamespaceUri)
        if not pref:
            continue
        prefix = str(pref).strip()
        if not prefix or re.fullmatch(r"default\d+", prefix):
            continue
        chosen = _namespace_from_vann_and_ontology(str(uri) if uri else "", ont)
        if chosen:
            # Vann-derived NS is authoritative; do not keep pattern-path bindings.
            by_prefix[prefix] = chosen

    if "" not in by_prefix:
        by_prefix[""] = master_ns
    return by_prefix


def _load_owl_imports(
    g: Graph,
    errors: list,
    catalog: dict | None = None,
    max_depth: int = 12,
    dev_map: dict | None = None,
) -> None:
    """
    Recursively materialize owl:imports into g.

    Resolution order for each import IRI:
      1. Already loaded (seen this IRI, or an owl:Ontology with that IRI is present)
      2. Per-user ``--dev`` IRI map (only when provided)
      3. OASIS XML catalog mapping (portable local remaps; no product-specific exceptions)
      4. Fetch / open the IRI itself (HTTP(S) or file URL)

    Failures are logged and recorded in errors so documentation can still proceed.
    Local classes for the MkDocs site remain limited to the project's master namespace.
    """
    if catalog is None:
        catalog = discover_oasis_catalogs()

    pending = {str(imp) for imp in g.objects(None, OWL.imports)}
    loaded = set()
    for ont in g.subjects(RDF.type, OWL.Ontology):
        loaded.add(str(ont))

    depth = 0
    while pending and depth < max_depth:
        depth += 1
        nxt = set()
        for imp_iri in sorted(pending):
            if imp_iri in loaded:
                continue
            loaded.add(imp_iri)

            if (URIRef(imp_iri), RDF.type, OWL.Ontology) in g:
                log.debug("owl:imports <%s> already present in graph", imp_iri)
                continue

            source = resolve_import_iri(imp_iri, catalog=catalog, dev_map=dev_map)
            before = len(g)
            try:
                _parse_import_source(g, source)
                log.info(
                    "Loaded owl:imports <%s> from %s — +%d triples (total %d)",
                    imp_iri,
                    source,
                    len(g) - before,
                    len(g),
                )
            except Exception as e:
                msg = f"Failed to load owl:imports <{imp_iri}> from {source}: {e}"
                errors.append(msg)
                log.warning(msg)
                continue

            for ont in g.subjects(RDF.type, OWL.Ontology):
                loaded.add(str(ont))
            for new_imp in g.objects(None, OWL.imports):
                s = str(new_imp)
                if s not in loaded:
                    nxt.add(s)
        pending = nxt


def process_ttl_files(ttl_files: list, errors: list, dev_map: dict | None = None) -> tuple:
    """
    Load ALL .ttl files into ONE unified graph, then follow owl:imports.
    Uses the TRUE master base namespace (from BASE) so local_classes works correctly.
    """
    g = Graph()

    for ttl_path in ttl_files:
        try:
            g.parse(ttl_path, format="turtle")
            log.info(f"Loaded {os.path.basename(ttl_path)} — total triples now {len(g)}")
        except Exception as e:
            error_msg = f"Failed to parse {ttl_path}: {str(e)}"
            errors.append(error_msg)
            log.error(error_msg)

    if len(g) == 0:
        raise ValueError("No triples loaded from any TTL file")

    catalog = discover_oasis_catalogs(search_roots=[os.path.dirname(p) for p in ttl_files])
    _load_owl_imports(g, errors, catalog=catalog, dev_map=dev_map)

    ns = _extract_master_namespace(ttl_files)
    log.info(f"Using master base namespace: {ns}")

    _rebind_preferred_prefixes(g, master_ns=ns)
    prefix_map = _build_prefix_map(g, ns)

    script_dir = os.path.dirname(os.path.realpath(__file__))
    registry = parse_concept_registry(script_dir)

    for uri, info in registry.items():
        u = URIRef(uri)
        if str(u).startswith(ns):
            if info["type"] == "object_property":
                g.add((u, RDF.type, OWL.ObjectProperty))
            elif info["type"] == "datatype_property":
                g.add((u, RDF.type, OWL.DatatypeProperty))

    classes = set(g.subjects(RDF.type, OWL.Class)) - {OWL.Thing}
    for shape in g.subjects(RDF.type, SH.NodeShape):
        target = g.value(shape, SH.targetClass)
        if target and isinstance(target, URIRef):
            classes.add(target)

    local_classes = [cls for cls in classes if str(cls).startswith(ns)]

    log.info(
        f"Collected {len(classes)} total classes, {len(local_classes)} local classes under master ns"
    )

    prop_map = {}
    for p in g.subjects(RDF.type, OWL.ObjectProperty):
        if str(p).startswith(ns):
            qn = get_qname(p, ns, prefix_map)
            prop_map[qn] = p
    for p in g.subjects(RDF.type, OWL.DatatypeProperty):
        if str(p).startswith(ns):
            qn = get_qname(p, ns, prefix_map)
            prop_map[qn] = p

    xsd_ns = "http://www.w3.org/2001/XMLSchema#"

    def _is_local(u) -> bool:
        return isinstance(u, URIRef) and str(u).startswith(ns)

    def _add_datatype_prop(p: URIRef):
        if not _is_local(p):
            return
        qn = get_qname(p, ns, prefix_map)
        if qn in prop_map:
            return
        prop_map[qn] = p
        g.add((p, RDF.type, OWL.DatatypeProperty))

    for p in g.subjects(RDFS.range, None):
        if not _is_local(p):
            continue
        for r in g.objects(p, RDFS.range):
            r_str = str(r)
            if r == RDFS.Literal or r_str.startswith(xsd_ns):
                _add_datatype_prop(p)
                break

    for shape in g.subjects(RDF.type, SH.NodeShape):
        for prop_shape in g.objects(shape, SH.property):
            path = g.value(prop_shape, SH.path)
            if not _is_local(path):
                continue

            shapes_to_check = [prop_shape] + list(g.objects(prop_shape, SH.node))
            for s in shapes_to_check:
                dt = g.value(s, SH.datatype)
                if dt is not None:
                    _add_datatype_prop(path)
                    break

    for uri, info in registry.items():
        if info["type"] in ("object_property", "datatype_property") and str(uri).startswith(ns):
            u = URIRef(uri)
            qn = get_qname(u, ns, prefix_map)
            prop_map[qn] = u

    return g, ns, prefix_map, classes, local_classes, prop_map
