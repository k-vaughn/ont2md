# ontology_processor_ttl.py
import os
import logging
import re
from rdflib import Graph, RDF, OWL, RDFS, URIRef
from rdflib.namespace import SH

from utils import (
    get_qname,
    discover_oasis_catalogs,
    resolve_iri_via_catalog,
)

log = logging.getLogger("ttl2mkdocs")


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
    """Parse a local path or URL into g, trying common RDF serializations."""
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
        else:
            g.parse(source)
        return

    # Remote / opaque IRI: try turtle then generic parse
    try:
        g.parse(source, format="turtle")
    except Exception:
        g.parse(source)


def _load_owl_imports(g: Graph, errors: list, catalog: dict | None = None, max_depth: int = 12) -> None:
    """
    Recursively materialize owl:imports into g.

    Resolution order for each import IRI:
      1. Already loaded (seen this IRI, or an owl:Ontology with that IRI is present)
      2. OASIS XML catalog mapping (portable local remaps; no product-specific exceptions)
      3. Fetch / open the IRI itself (HTTP(S) or file URL)

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

            source = resolve_iri_via_catalog(imp_iri, catalog) or imp_iri
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


def process_ttl_files(ttl_files: list, errors: list) -> tuple:
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
    _load_owl_imports(g, errors, catalog=catalog)

    ns = _extract_master_namespace(ttl_files)
    log.info(f"Using master base namespace: {ns}")

    prefix_map = dict(g.namespaces())
    if ns not in prefix_map:
        prefix_map[ns] = ":"

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
