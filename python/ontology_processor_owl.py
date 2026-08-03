# ontology_processor_owl.py
"""Load RDF/XML (.owl) sources into one graph — parallels ontology_processor_ttl."""
import os
import logging
import re
from rdflib import Graph, RDF, OWL, RDFS, URIRef
from rdflib.namespace import SH

from utils import (
    get_qname,
    parse_concept_registry,
    discover_oasis_catalogs,
    apply_registry_types_for_present_terms,
)
from ontology_processor_ttl import (
    _load_owl_imports,
    _rebind_preferred_prefixes,
    _build_prefix_map,
    _update_registries_from_graph,
    _normalize_master_namespace,
)

log = logging.getLogger("owl2mkdocs")


def _preprocess_owl_xml(content: str) -> str:
    """Fix common Protégé / relative-IRI quirks before RDFLib XML parse."""
    content = content.replace(" xml:", " xmlns:")
    content = content.replace('rdf:about=":', 'rdf:about="')
    content = content.replace('rdf:resource=":', 'rdf:resource="')
    return content


def _extract_master_namespace(owl_files: list) -> str:
    """
    Find the master base namespace, in priority order:

      1. ``xml:base``
      2. ``vann:preferredNamespaceUri``
      3. Default ``xmlns="..."`` (empty-prefix equivalent)
      4. ``owl:Ontology`` ``rdf:about``
      5. Fallback ``https://example.org/``
    """
    contents: list[tuple[str, str]] = []
    for owl_path in owl_files:
        try:
            with open(owl_path, "r", encoding="utf-8") as f:
                contents.append((owl_path, f.read()))
        except Exception as e:
            log.warning(f"Could not read {owl_path} for namespace: {e}")

    for owl_path, content in contents:
        base_match = re.search(r'xml:base\s*=\s*"([^"]+)"', content, re.IGNORECASE)
        if base_match:
            ns = _normalize_master_namespace(base_match.group(1))
            log.debug(f"Found master namespace from xml:base in {owl_path}: {ns}")
            return ns

    for owl_path, content in contents:
        pref_match = re.search(
            r"preferredNamespaceUri[^>]*(?:>([^<\s]+)|rdf:resource=\"([^\"]+)\")",
            content,
            re.IGNORECASE,
        )
        if pref_match:
            raw = pref_match.group(1) or pref_match.group(2)
            ns = _normalize_master_namespace(raw.strip())
            log.info(f"Found master namespace from vann:preferredNamespaceUri in {owl_path}: {ns}")
            return ns

    for owl_path, content in contents:
        # Default xmlns (empty prefix) — not xmlns:prefix=
        empty_match = re.search(r'(?:^|\s)xmlns\s*=\s*"([^"]+)"', content)
        if empty_match:
            ns = _normalize_master_namespace(empty_match.group(1))
            log.info(f"Found master namespace from default xmlns in {owl_path}: {ns}")
            return ns

    for owl_path, content in contents:
        # <owl:Ontology rdf:about="..."> or <Ontology rdf:about="...">
        about_match = re.search(
            r"<(?:[\w.-]+:)?Ontology\b[^>]*\brdf:about\s*=\s*\"([^\"]+)\"",
            content,
            re.IGNORECASE,
        )
        if about_match:
            ns = _normalize_master_namespace(about_match.group(1))
            log.info(f"Found master namespace from owl:Ontology rdf:about in {owl_path}: {ns}")
            return ns

    log.warning(
        "No xml:base, vann:preferredNamespaceUri, default xmlns, or owl:Ontology rdf:about "
        "found – using https://example.org/"
    )
    return "https://example.org/"


def parse_owl_file(g: Graph, owl_path: str) -> None:
    """Parse one .owl RDF/XML file into g (with preprocessing)."""
    with open(owl_path, "r", encoding="utf-8") as f:
        content = _preprocess_owl_xml(f.read())
    g.parse(data=content, format="xml", publicID=os.path.abspath(owl_path))


def process_owl_files(owl_files: list, errors: list, dev_map: dict | None = None) -> tuple:
    """
    Load ALL .owl files into ONE unified graph, then follow owl:imports.
    Mirrors process_ttl_files.
    """
    g = Graph()

    for owl_path in owl_files:
        try:
            parse_owl_file(g, owl_path)
            log.info(f"Loaded {os.path.basename(owl_path)} — total triples now {len(g)}")
        except Exception as e:
            error_msg = f"Failed to parse {owl_path}: {str(e)}"
            errors.append(error_msg)
            log.error(error_msg)

    if len(g) == 0:
        raise ValueError("No triples loaded from any OWL file")

    catalog = discover_oasis_catalogs(search_roots=[os.path.dirname(p) for p in owl_files])
    _load_owl_imports(g, errors, catalog=catalog, dev_map=dev_map)

    ns = _extract_master_namespace(owl_files)
    log.info(f"Using master base namespace: {ns}")

    _rebind_preferred_prefixes(g, master_ns=ns)
    prefix_map = _build_prefix_map(g, ns)

    script_dir = os.path.dirname(os.path.realpath(__file__))
    registry = parse_concept_registry(script_dir)
    apply_registry_types_for_present_terms(g, registry, ns)

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

    datatype_map = {}
    for dt in g.subjects(RDF.type, RDFS.Datatype):
        if isinstance(dt, URIRef) and str(dt).startswith(ns):
            qn = get_qname(dt, ns, prefix_map)
            datatype_map[qn] = dt

    xsd_ns = "http://www.w3.org/2001/XMLSchema#"

    def _is_local(u) -> bool:
        return isinstance(u, URIRef) and str(u).startswith(ns)

    def _add_datatype_prop(p: URIRef):
        if not _is_local(p):
            return
        qn = get_qname(p, ns, prefix_map)
        if qn in prop_map or qn in datatype_map:
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

    _update_registries_from_graph(g, ns, owl_files, script_dir)

    return g, ns, prefix_map, classes, local_classes, prop_map, datatype_map
