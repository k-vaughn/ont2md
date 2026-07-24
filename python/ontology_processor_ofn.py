# ontology_processor_ofn.py
"""Load OWL Functional Syntax (.ofn) sources into one graph — parallels ontology_processor_ttl."""
import os
import logging
import re
from rdflib import Graph, RDF, OWL, RDFS, URIRef
from rdflib.namespace import SH

from utils import (
    get_qname,
    parse_concept_registry,
    discover_oasis_catalogs,
    get_prefix_named_pairs,
)
from ontology_processor_ttl import (
    _load_owl_imports,
    _rebind_preferred_prefixes,
    _build_prefix_map,
    _update_registries_from_graph,
    _normalize_master_namespace,
)

log = logging.getLogger("ofn2mkdocs")


def _funowl_to_python():
    try:
        from funowl.converters.functional_converter import to_python
    except ImportError as e:
        raise ImportError(
            "OFN support requires the 'funowl' package. Install with: pip install funowl"
        ) from e
    return to_python


def _strip_unsupported_swrl(content: str, ofn_path: str) -> str:
    """Remove DLSafeRule blocks — funowl does not support SWRL."""
    if "DLSafeRule" not in content:
        return content
    content = content.rsplit("DLSafeRule", 1)[0].rstrip() + ")"
    log.info(
        "Removed unsupported DLSafeRule from %s as funowl does not support SWRL rules.",
        ofn_path,
    )
    return content


def _extract_master_namespace(ofn_files: list) -> str:
    """
    Find the master base namespace, in priority order:

      1. ``vann:preferredNamespaceUri`` annotation
      2. Empty prefix ``Prefix(: <iri>)``
      3. ``Ontology(<iri>`` (``rdf:about`` equivalent; any Ontology IRI)
      4. Fallback ``https://example.org/``
    """
    contents: list[tuple[str, str]] = []
    for ofn_path in ofn_files:
        try:
            with open(ofn_path, "r", encoding="utf-8") as f:
                contents.append((ofn_path, f.read()))
        except Exception as e:
            log.warning(f"Could not read {ofn_path} for namespace: {e}")

    for ofn_path, content in contents:
        pref_match = re.search(
            r"preferredNamespaceUri\s+(?:<([^>]+)>|\"([^\"]+)\"|'([^']+)')",
            content,
        )
        if pref_match:
            ns = _normalize_master_namespace(
                pref_match.group(1) or pref_match.group(2) or pref_match.group(3)
            )
            log.info(f"Found master namespace from vann:preferredNamespaceUri in {ofn_path}: {ns}")
            return ns

    for ofn_path, content in contents:
        empty_match = re.search(r"Prefix\s*\(\s*:\s*<([^>]+)>", content)
        if empty_match:
            ns = _normalize_master_namespace(empty_match.group(1))
            log.info(f"Found master namespace from empty Prefix(:) in {ofn_path}: {ns}")
            return ns

    for ofn_path, content in contents:
        ont_match = re.search(r"Ontology\s*\(\s*<([^>]+)>", content)
        if ont_match:
            ns = _normalize_master_namespace(ont_match.group(1).strip())
            log.info(f"Found master namespace from Ontology IRI in {ofn_path}: {ns}")
            return ns

    log.warning(
        "No vann:preferredNamespaceUri, empty Prefix(:), or Ontology IRI "
        "found – using https://example.org/"
    )
    return "https://example.org/"


def parse_ofn_file(g: Graph, ofn_path: str) -> None:
    """Parse one .ofn file into g via funowl."""
    to_python = _funowl_to_python()
    with open(ofn_path, "r", encoding="utf-8") as f:
        content = _strip_unsupported_swrl(f.read(), ofn_path)
    doc = to_python(content)
    if not doc:
        raise ValueError("Failed to parse OWL functional syntax document")
    # Bind prefixes from the document before converting
    try:
        ont_iri = None
        if hasattr(doc, "ontology") and doc.ontology and doc.ontology.iri:
            ont_iri = str(doc.ontology.iri)
        prefix_pairs = get_prefix_named_pairs(doc, ont_iri or "https://example.org/")
        for item in prefix_pairs:
            prefix = (item.get("prefix") or "").rstrip(":")
            uri = item.get("uri")
            if uri:
                g.bind(prefix, URIRef(uri))
    except Exception as e:
        log.debug("Could not bind OFN prefixes from %s: %s", ofn_path, e)
    doc.to_rdf(g)


def process_ofn_files(ofn_files: list, errors: list, dev_map: dict | None = None) -> tuple:
    """
    Load ALL .ofn files into ONE unified graph, then follow owl:imports.
    Mirrors process_ttl_files.
    """
    g = Graph()

    for ofn_path in ofn_files:
        try:
            parse_ofn_file(g, ofn_path)
            log.info(f"Loaded {os.path.basename(ofn_path)} — total triples now {len(g)}")
        except Exception as e:
            error_msg = (
                f"Failed to parse {ofn_path}: {e}\n"
                "Ensure the 'funowl' library is installed (`pip install funowl`) "
                "and the .ofn file is valid."
            )
            errors.append(error_msg)
            log.error(error_msg)

    if len(g) == 0:
        raise ValueError("No triples loaded from any OFN file")

    catalog = discover_oasis_catalogs(search_roots=[os.path.dirname(p) for p in ofn_files])
    _load_owl_imports(g, errors, catalog=catalog, dev_map=dev_map)

    ns = _extract_master_namespace(ofn_files)
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

    for uri, info in registry.items():
        if info["type"] in ("object_property", "datatype_property") and str(uri).startswith(ns):
            u = URIRef(uri)
            qn = get_qname(u, ns, prefix_map)
            prop_map[qn] = u

    _update_registries_from_graph(g, ns, ofn_files, script_dir)

    return g, ns, prefix_map, classes, local_classes, prop_map, datatype_map
