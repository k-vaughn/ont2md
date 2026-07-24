# reqview_csv_generator.py
import os
import csv
import logging
from datetime import datetime
from rdflib import Graph, Literal
from rdflib.namespace import RDF, OWL, VANN

from utils import get_qname, get_label, get_reqview_id, get_definition, get_title

log = logging.getLogger("ttl2mkdocs")


def generate_reqview_update_csv(g: Graph, local_classes: list, ns: str, prefix_map: dict, docs_dir: str, create_missing: bool = False):
    """
    Generate ONE ReqView update CSV for the current namespace.
    Now includes BOTH classes and properties.
    type column = "Class", "ObjectProperty", or "DatatypeProperty" (without owl: prefix).
    """
    traceability_dir = os.path.join(docs_dir, "traceability")
    os.makedirs(traceability_dir, exist_ok=True)

    # Determine namespace prefix from vann:preferredNamespacePrefix or first qname
    preferred_prefix = None
    for ont in g.subjects(RDF.type, OWL.Ontology):
        pref = g.value(ont, VANN.preferredNamespacePrefix)
        if pref and isinstance(pref, Literal):
            preferred_prefix = str(pref).strip()
            break

    if not preferred_prefix:
        for item in local_classes:
            qname = get_qname(item, ns, prefix_map)
            if qname and ':' in qname:
                preferred_prefix = qname.split(':', 1)[0]
                break

    if not preferred_prefix or preferred_prefix == "":
        preferred_prefix = "ontology"

    csv_filename = f"{preferred_prefix}.csv"
    csv_path = os.path.join(traceability_dir, csv_filename)

    fieldnames = [
        "id",
        "qname",
        "heading",
        "text",
        "type",           # "Class", "ObjectProperty", or "DatatypeProperty"
        "diagram",
        "website",
        "updated"
    ]

    rows = []
    now = datetime.now().isoformat()

    # === Process Classes ===
    for cls in local_classes:
        qname = get_qname(cls, ns, prefix_map)
        if not qname or qname in ('ITSThing', 'TimeThing'):
            continue

        reqview_id = get_reqview_id(g, cls, ns, prefix_map)

        if not create_missing and not reqview_id:
            continue

        heading = get_title(g, cls) or get_label(g, cls) or qname
        text = get_definition(g, cls) or ""

        rows.append({
            "id": reqview_id or "",
            "qname": qname,
            "heading": heading,
            "text": text[:500],
            "type": "Class",
            "diagram": f"{ns}diagrams/{qname}.dot.svg",
            "website": f"{ns}{qname}",
            "updated": now
        })

    # === Process Object Properties ===
    for prop in g.subjects(RDF.type, OWL.ObjectProperty):
        if not str(prop).startswith(ns):
            continue
        qname = get_qname(prop, ns, prefix_map)
        if not qname:
            continue

        reqview_id = get_reqview_id(g, prop, ns, prefix_map)

        if not create_missing and not reqview_id:
            continue

        heading = get_title(g, prop) or get_label(g, prop) or qname
        text = get_definition(g, prop) or ""

        rows.append({
            "id": reqview_id or "",
            "qname": qname,
            "heading": heading,
            "text": text[:500],
            "type": "ObjectProperty",
            "diagram": "",                    # properties usually don't have diagrams yet
            "website": f"{ns}{qname}",          # you can generate property pages later if desired
            "updated": now
        })

    # === Process Datatype Properties ===
    for prop in g.subjects(RDF.type, OWL.DatatypeProperty):
        if not str(prop).startswith(ns):
            continue
        qname = get_qname(prop, ns, prefix_map)
        if not qname:
            continue

        reqview_id = get_reqview_id(g, prop, ns, prefix_map)

        if not create_missing and not reqview_id:
            continue

        heading = get_title(g, prop) or get_label(g, prop) or qname
        text = get_definition(g, prop) or ""

        rows.append({
            "id": reqview_id or "",
            "qname": qname,
            "heading": heading,
            "text": text[:500],
            "type": "DatatypeProperty",
            "diagram": "",
            "website": f"{ns}{qname}",
            "updated": now
        })

    if not rows:
        log.warning(f"No concepts with ReqView IDs found for namespace {preferred_prefix}")
        return

    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    mode = "Update + Create" if create_missing else "Update only"
    log.info(f"Generated {csv_filename} with {len(rows)} rows [{mode}] (classes + properties)")
    print(f"✅ ReqView update CSV ready: {csv_path}")
    print(f"   Mode: {mode} | Items: {len(rows)}")
    print(f"   Import into your '{preferred_prefix}' document in ReqView using 'Update existing objects' mode.")