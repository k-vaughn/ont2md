# ttl2md.py
import os
import sys
import logging
import traceback
from collections import defaultdict
from rdflib import Graph, RDF, OWL, URIRef
from rdflib.namespace import DCTERMS, SKOS, DC, SH, VANN
from rdflib.plugins.parsers.notation3 import BadSyntax

from ontology_processor_ttl import process_ttl_files
from diagram_generator import generate_diagram
from markdown_generator import (
    generate_markdown, update_mkdocs_nav, generate_index,
    generate_pattern_markdown_file,generate_property_markdown
)
from utils import (
    get_qname, get_label, is_abstract, get_id,
    get_ontology_metadata, insert_spaces, get_preferred_prefix,
    resolve_home_ontology,
)
from reqview_csv_generator import generate_reqview_update_csv

# -------------------- logging --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s"
)
log = logging.getLogger("ttl2mkdocs")

def _format_syntax_context(path: str, line_no: int | None, window: int = 4) -> str:
    if not line_no or line_no <= 0:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        start = max(1, line_no - window)
        end = min(len(lines), line_no + window)
        out = [f"Context from {os.path.basename(path)}:"]
        for i in range(start, end + 1):
            marker = ">>" if i == line_no else "  "
            out.append(f"{marker} {i:>5} | {lines[i-1].rstrip()}")
        return "\n".join(out)
    except Exception:
        return ""


def main():
    """Main entry point for TTL-based ontology → MkDocs + ODM diagrams."""
    log.debug("Starting ttl2md.py (TTL + SHACL support)")

    # Handle optional flags
    create_missing = False
    if "--create-missing" in sys.argv or "-c" in sys.argv:
        create_missing = True
        # Remove the flag from sys.argv so it doesn't interfere with other logic
        sys.argv = [arg for arg in sys.argv if arg not in ("--create-missing", "-c")]

    # Basic usage check (now allows the flag)
    if len(sys.argv) != 1:
        print("Usage: python ttl2md.py [--create-missing | -c]")
        print("       --create-missing, -c   Include concepts without ReqView ID (will create new objects in ReqView)")
        sys.exit(1)

    root_dir = os.getcwd()
    mkdocs_path = os.path.join(root_dir, "mkdocs.yml")
    docs_dir = os.path.join(root_dir, "docs")

    if not os.path.exists(mkdocs_path):
        print("Error: mkdocs.yml not found")
        sys.exit(1)
    if not os.path.isdir(docs_dir):
        print("Error: docs directory not found")
        sys.exit(1)

    # Create diagrams directory
    diagrams_dir = os.path.join(docs_dir, "diagrams")
    os.makedirs(diagrams_dir, exist_ok=True)
    log.debug(f"Diagrams directory: {diagrams_dir}")

    # Find all .ttl files
    ttl_files = [os.path.join(docs_dir, f) for f in os.listdir(docs_dir)
                 if f.lower().endswith('.ttl')]
    if not ttl_files:
        print("No .ttl files found in docs/")
        sys.exit(0)

    errors = []
    processed_count = 0

    # === 1. Load ALL TTL files into one unified graph ===
    try:
        g, ns, prefix_map, all_classes, local_classes, prop_map = process_ttl_files(ttl_files, errors)
    except Exception as e:
        log.error(f"Failed to process TTL files: {e}")
        sys.exit(1)

    log.debug(f"Unified graph ready — {len(g)} triples, {len(local_classes)} local classes")

    # Global collections
    global_all_classes = {get_qname(c, ns, prefix_map) for c in all_classes if c != OWL.Thing}
    abstract_map = {get_qname(c, ns, prefix_map): is_abstract(c, g, ns) for c in all_classes}
    class_to_onts = defaultdict(list)
    ns_to_ontology = {ns: "FuzzyTime"}  # adjust if you have multiple patterns

    # === 2. Build ontology_info (one entry per pattern file) ===
    # Each *-pattern.ttl file becomes its own pattern.
    # We load each file *individually* so we can see exactly which classes it declares.
    ontology_info = {}
    class_to_onts = defaultdict(set)          # class_name → set of pattern names that define it

    for ttl_path in ttl_files:
        base_name = os.path.splitext(os.path.basename(ttl_path))[0]
        if base_name.endswith('-shacl'):
            continue

        ont_name = base_name.replace('-pattern', '')   # e.g. fuzzy-time-pattern.ttl → fuzzy-time

        # === Load THIS file alone to discover its direct classes ===
        temp_g = Graph()
        try:
            temp_g.parse(ttl_path, format="turtle")
        except BadSyntax as e:
            line_no = getattr(e, "lines", None)
            col = getattr(e, "column", None)
            msg = str(e) if str(e) else "BadSyntax while parsing Turtle"
            ctx = _format_syntax_context(ttl_path, line_no)
            loc = f"line {line_no}" + (f", col {col}" if col is not None else "") if line_no else "unknown location"
            log.error("Error parsing TTL file %s at %s.\n%s\n%s", ttl_path, loc, msg, ctx)
            sys.exit(2)
        except Exception as e:
            log.error("Error parsing TTL file %s (%s)", ttl_path, str(e))
            sys.exit(2)

        # === Determine ontology module name (case-sensitive) for filenames ===
        # We prefer the ontology IRI local name, e.g. .../AreaPattern → "AreaPattern"
        module_name = None
        for ont_iri in temp_g.subjects(RDF.type, OWL.Ontology):
            if isinstance(ont_iri, URIRef):
                module_name = str(ont_iri).split("/")[-1].split("#")[-1]
                if module_name:
                    break
        if not module_name:
            module_name = ont_name

        direct_classes = set()
        for s in temp_g.subjects(RDF.type, OWL.Class):
            if isinstance(s, URIRef):
                cls_name = get_label(temp_g, s) or get_qname(s, ns, prefix_map)
#                if cls_name not in ('ITSThing', 'TimeThing'):
                direct_classes.add(cls_name)

        # Direct properties defined in this module (used for nav grouping)
        direct_properties = set()
        for p in temp_g.subjects(RDF.type, OWL.ObjectProperty):
            if isinstance(p, URIRef) and str(p).startswith(ns):
                direct_properties.add(get_qname(p, ns, prefix_map))
        for p in temp_g.subjects(RDF.type, OWL.DatatypeProperty):
            if isinstance(p, URIRef) and str(p).startswith(ns):
                direct_properties.add(get_qname(p, ns, prefix_map))

        # === Metadata for this pattern ===
        title = get_ontology_metadata(temp_g, ns, DCTERMS.title) or insert_spaces(ont_name)
        desc = (get_ontology_metadata(temp_g, ns, SKOS.definition) or
                get_ontology_metadata(temp_g, ns, DCTERMS.description) or "")
        is_draft = get_ontology_metadata(temp_g, ns,
            URIRef("https://w3id.org/itsdata/core/v1/draft")) or "false"
        prefix = get_ontology_metadata(temp_g, ns, VANN.preferredNamespacePrefix)

        ontology_info[ont_name] = {
            "title": title,
            "full_title": title,
            "description": desc,
            "classes": direct_classes,          # ← only classes defined in THIS file
            "properties": sorted(direct_properties),
            "imports": [],                      # filled below if needed
            "draft": is_draft.lower() == "true",
            "file": ttl_path,                    # for debugging
            "module_name": module_name,          # used for pattern page filename + capitalization
            "prefix": prefix if prefix else ont_name  # for navigation grouping
        }

        # Record which pattern owns each class
        for cls_name in direct_classes:
            class_to_onts[cls_name].add(ont_name)

    # ===  Collect DIRECT imports for each pattern (only from its own file) ===
    for ont_name, ont in ontology_info.items():
        ttl_path = ont["file"]
        temp_g = Graph()
        temp_g.parse(ttl_path, format="turtle")

        direct_imports = []   

        for ont_iri in temp_g.subjects(RDF.type, OWL.Ontology):
            for imported in temp_g.objects(ont_iri, OWL.imports):
                imp_str = str(imported).strip()
                direct_imports.append(imp_str)

        # Deduplicate and sort
        ont["imports"] = sorted(set(direct_imports))

    log.debug(f"Built ontology_info with {len(ontology_info)} patterns")
    for name, data in ontology_info.items():
        log.debug(f"  • {name}: {len(data['classes'])} direct classes")


    # === 3. Generate diagrams + Markdown for every class ===
    for cls in sorted(local_classes, key=lambda u: get_label(g, u).lower()):
        cls_name = get_label(g, cls)
#        if cls_name in ('ITSThing', 'TimeThing'):
#            continue

        cls_id = get_id(cls_name.replace(":", "_"))
        log.debug(f"Processing class: {cls_name}")

        try:
            # Generate ODM-style diagram (OWL + SHACL merged)
            generate_diagram(
                g, cls, cls_name, cls_id, ns,
                global_all_classes, abstract_map,
                "dummy.ttl", errors, prefix_map,
                list(ontology_info.keys())[0] if ontology_info else "",
                ns_to_ontology
            )

            # Generate Markdown page
            generate_markdown(
                g, cls, cls_name, global_all_classes, ns, docs_dir,
                errors, prefix_map, ns_to_ontology, class_to_onts,
                ontology_info[list(ontology_info.keys())[0]]["draft"] if ontology_info else False
            )
            processed_count += 1

        except Exception as e:
            error_msg = f"Error processing class {cls_name}: {str(e)}\n{traceback.format_exc()}"
            errors.append(error_msg)
            log.error(error_msg)

    # === 4. Generate property documentation pages ===
    prop_dir = os.path.join(docs_dir, "properties")
    os.makedirs(prop_dir, exist_ok=True)

    for prop_qname, prop_uri in prop_map.items():   # prop_map from process_ttl_files
        if str(prop_uri).startswith(ns):            # only local properties
            generate_property_markdown(
                g, prop_uri, prop_qname, ns, prefix_map, 
                docs_dir, global_all_classes,
                ontology_info[list(ontology_info.keys())[0]]["draft"] 
                if ontology_info else False
            )

    # === 5. Generate index + pattern overview pages ===
    preferred_prefix = get_preferred_prefix(g)
    home_ont_name = resolve_home_ontology(ontology_info, preferred_prefix)
    index_generated = False
    for ont_name, ont in ontology_info.items():
        log.debug(f"Generating overview for pattern: {ont_name} (preferred prefix: {preferred_prefix})")
        if ont_name.endswith('-reqview'):
            continue
        if ont_name == home_ont_name:
            generate_index(g, ont_name, ns, prefix_map, ont, docs_dir, ontology_info, errors, class_to_onts, ont["draft"] if ont else True)
            index_generated = True
        else:
            generate_pattern_markdown_file(g, ont_name, ns, prefix_map, ont, docs_dir, class_to_onts, ontology_info)

    if not index_generated and home_ont_name and home_ont_name in ontology_info:
        ont = ontology_info[home_ont_name]
        generate_index(g, home_ont_name, ns, prefix_map, ont, docs_dir, ontology_info, errors, class_to_onts, ont["draft"] if ont else True)

    # === 6. Update MkDocs navigation ===
    try:
        update_mkdocs_nav(mkdocs_path, ontology_info, global_all_classes, errors,
                          class_to_onts, ontology_info, ttl_files)
    except Exception as e:
        error_msg = f"Error updating mkdocs.yml: {str(e)}\n{traceback.format_exc()}"
        errors.append(error_msg)
        log.error(error_msg)

    log.info(f"Finished — processed {processed_count} classes")
    if errors:
        log.error("Errors encountered:")
        for err in errors:
            log.error(err)

    # === 7. Generate ReqView update CSV for safe manual import ===
    try:
        generate_reqview_update_csv(g, local_classes, ns, prefix_map, docs_dir, create_missing)
    except Exception as e:
        error_msg = f"Error generating ReqView update CSV: {str(e)}\n{traceback.format_exc()}"
        errors.append(error_msg)
        log.error(error_msg)

if __name__ == "__main__":
    main()