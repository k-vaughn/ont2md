# owl2md.py
import os
import sys
import logging
import shutil
import traceback
from collections import defaultdict
from rdflib import Graph, Namespace, RDF, RDFS, OWL, URIRef
from rdflib.namespace import VANN

from ontology_processor_owl import process_owl_files, parse_owl_file
from diagram_generator import generate_diagram
from markdown_generator import (
    generate_markdown, update_mkdocs_nav, generate_index,
    generate_pattern_markdown_file, generate_property_markdown,
    generate_datatype_markdown,
)
from utils import (
    get_qname, get_label, is_abstract, get_id,
    get_ontology_metadata, get_ontology_title, get_ontology_description, insert_spaces, get_preferred_prefix,
    get_ontology_notes, get_ontology_copyright, get_ontology_license,
    resolve_home_ontology, load_dev_iri_map, find_dev_iri_map_path,
    describe_dev_iri_map_search, is_shacl_or_alignment_ttl, pattern_module_key,
)
from reqview_csv_generator import generate_reqview_update_csv

CDM1 = Namespace("https://w3id.org/citydata/part1/v1/")

# -------------------- logging --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s"
)
log = logging.getLogger("owl2mkdocs")


def _reset_generated_output_dirs(docs_dir: str) -> None:
    """Remove and recreate generated output dirs so stale pages/diagrams are not left behind."""
    for name in ("classes", "properties", "datatypes", "diagrams"):
        path = os.path.join(docs_dir, name)
        if os.path.isdir(path):
            shutil.rmtree(path)
            log.info(f"Cleared {path}")
        os.makedirs(path, exist_ok=True)


def main():
    """Main entry point for OWL (RDF/XML) → MkDocs + ODM diagrams."""
    log.debug("Starting owl2md.py (OWL + SHACL support)")

    create_missing = False
    if "--create-missing" in sys.argv or "-c" in sys.argv:
        create_missing = True
        sys.argv = [arg for arg in sys.argv if arg not in ("--create-missing", "-c")]

    dev_mode = False
    if "--dev" in sys.argv or "-dev" in sys.argv:
        dev_mode = True
        sys.argv = [arg for arg in sys.argv if arg not in ("--dev", "-dev")]

    dev_map_path = None
    if "--dev-map" in sys.argv:
        idx = sys.argv.index("--dev-map")
        if idx + 1 >= len(sys.argv):
            print("Error: --dev-map requires a file path")
            sys.exit(1)
        dev_map_path = sys.argv[idx + 1]
        dev_mode = True
        del sys.argv[idx:idx + 2]

    if len(sys.argv) != 1:
        print("Usage: python owl2md.py [--create-missing | -c] [--dev | -dev] [--dev-map PATH]")
        print("       --create-missing, -c   Include concepts without ReqView ID (will create new objects in ReqView)")
        print("       --dev, -dev            Remap owl:imports via a local per-user map file (not published IRIs)")
        print("       --dev-map PATH         Path to IRI map (implies --dev); default: ./dev-iri-map.yml")
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

    diagrams_dir = os.path.join(docs_dir, "diagrams")
    os.makedirs(diagrams_dir, exist_ok=True)
    log.debug(f"Diagrams directory: {diagrams_dir}")

    owl_files = [os.path.join(docs_dir, f) for f in os.listdir(docs_dir)
                 if f.lower().endswith(".owl")]
    if not owl_files:
        print("No .owl files found in docs/")
        sys.exit(0)

    errors = []
    processed_count = 0

    dev_map = None
    if dev_mode:
        map_file = find_dev_iri_map_path(dev_map_path, search_roots=[root_dir, docs_dir])
        if not map_file:
            print("Error: --dev requires a local IRI map file.")
            print("       Copy dev-iri-map.example.yml → dev-iri-map.yml (gitignored),")
            print("       or place one at ~/.config/ont2md/dev-iri-map.yml,")
            print("       or pass --dev-map PATH")
            print("       Looked for:")
            for p in describe_dev_iri_map_search(search_roots=[root_dir, docs_dir])[:12]:
                print(f"         - {p}")
            sys.exit(1)
        if "example" in os.path.basename(map_file).lower():
            print(
                f"Note: using example map {map_file}\n"
                f"      Copy it to dev-iri-map.yml (or ~/.config/ont2md/dev-iri-map.yml) "
                f"for your personal paths."
            )
        try:
            dev_map = load_dev_iri_map(map_file)
        except Exception as e:
            print(f"Error loading dev IRI map: {e}")
            sys.exit(1)
        banner = (
            f"*** DEV MODE *** Remapping owl:imports via {map_file}\n"
            f"                 Published w3id/HTTP sources are NOT used for mapped IRIs.\n"
            f"                 Omit --dev to fetch the live published ontologies."
        )
        print(banner)
        log.warning(banner)

    # === 1. Load ALL OWL files into one unified graph ===
    try:
        g, ns, prefix_map, all_classes, local_classes, prop_map, datatype_map = process_owl_files(
            owl_files, errors, dev_map=dev_map
        )
    except Exception as e:
        log.error(f"Failed to process OWL files: {e}")
        sys.exit(1)

    log.debug(f"Unified graph ready — {len(g)} triples, {len(local_classes)} local classes")

    global_all_classes = {get_qname(c, ns, prefix_map) for c in all_classes if c != OWL.Thing}
    global_all_datatypes = set(datatype_map.keys())
    global_all_properties = set(prop_map.keys())
    abstract_map = {get_qname(c, ns, prefix_map): is_abstract(c, g, ns) for c in all_classes}
    class_to_onts = defaultdict(set)
    ns_to_ontology = {ns: "Ontology"}

    # === 2. Build ontology_info (one entry per module file) ===
    ontology_info = {}

    for owl_path in owl_files:
        base_name = os.path.splitext(os.path.basename(owl_path))[0]
        if is_shacl_or_alignment_ttl(owl_path):
            continue

        ont_name = pattern_module_key(base_name)

        temp_g = Graph()
        try:
            parse_owl_file(temp_g, owl_path)
        except Exception as e:
            log.error("Error parsing OWL file %s (%s)", owl_path, str(e))
            sys.exit(2)

        module_name = None
        for ont_iri in temp_g.subjects(RDF.type, OWL.Ontology):
            if isinstance(ont_iri, URIRef):
                module_name = str(ont_iri).split("/")[-1].split("#")[-1]
                if module_name:
                    break
        if not module_name:
            module_name = base_name if base_name.endswith("Pattern") else ont_name

        direct_classes = set()
        for s in temp_g.subjects(RDF.type, OWL.Class):
            if isinstance(s, URIRef) and str(s).startswith(ns):
                cls_name = get_label(temp_g, s) or get_qname(s, ns, prefix_map)
                direct_classes.add(cls_name)

        direct_properties = set()
        for p in temp_g.subjects(RDF.type, OWL.ObjectProperty):
            if isinstance(p, URIRef) and str(p).startswith(ns):
                direct_properties.add(get_qname(p, ns, prefix_map))
        for p in temp_g.subjects(RDF.type, OWL.DatatypeProperty):
            if isinstance(p, URIRef) and str(p).startswith(ns):
                direct_properties.add(get_qname(p, ns, prefix_map))
        direct_datatypes = set()
        for dt in temp_g.subjects(RDF.type, RDFS.Datatype):
            if isinstance(dt, URIRef) and str(dt).startswith(ns):
                direct_datatypes.add(get_qname(dt, ns, prefix_map))

        title = get_ontology_title(temp_g, ns) or insert_spaces(ont_name)
        desc = get_ontology_description(temp_g, ns) or ""
        is_draft = get_ontology_metadata(temp_g, ns,
            URIRef("https://w3id.org/itsdata/core/v1/draft")) or "false"
        prefix = get_ontology_metadata(temp_g, ns, VANN.preferredNamespacePrefix)
        is_main_module = (get_ontology_metadata(temp_g, ns, CDM1.mainModule) or "").lower() == "true"

        ontology_info[ont_name] = {
            "title": title,
            "full_title": title,
            "description": desc,
            "notes": get_ontology_notes(temp_g),
            "copyright": get_ontology_copyright(temp_g),
            "license": get_ontology_license(temp_g),
            "classes": direct_classes,
            "properties": sorted(direct_properties),
            "datatypes": sorted(direct_datatypes),
            "imports": [],
            "draft": is_draft.lower() == "true",
            "file": owl_path,
            "module_name": module_name,
            "prefix": prefix if prefix else ont_name,
            "main_module": is_main_module,
        }

        for cls_name in direct_classes:
            class_to_onts[cls_name].add(ont_name)

    for ont_name, ont in ontology_info.items():
        temp_g = Graph()
        parse_owl_file(temp_g, ont["file"])
        direct_imports = []
        for ont_iri in temp_g.subjects(RDF.type, OWL.Ontology):
            for imported in temp_g.objects(ont_iri, OWL.imports):
                direct_imports.append(str(imported).strip())
        ont["imports"] = sorted(set(direct_imports))

    log.debug(f"Built ontology_info with {len(ontology_info)} patterns")

    _reset_generated_output_dirs(docs_dir)

    # === 3. Generate diagrams + Markdown for every class ===
    for cls in sorted(local_classes, key=lambda u: get_label(g, u).lower()):
        cls_name = get_label(g, cls)
        cls_id = get_id(cls_name.replace(":", "_"))
        log.debug(f"Processing class: {cls_name}")

        try:
            generate_diagram(
                g, cls, cls_name, cls_id, ns,
                global_all_classes, abstract_map,
                "dummy.owl", errors, prefix_map,
                list(ontology_info.keys())[0] if ontology_info else "",
                ns_to_ontology
            )
            generate_markdown(
                g, cls, cls_name, global_all_classes, ns, docs_dir,
                errors, prefix_map, ns_to_ontology, class_to_onts,
                ontology_info[list(ontology_info.keys())[0]]["draft"] if ontology_info else False,
                global_all_datatypes=global_all_datatypes,
                global_all_properties=global_all_properties,
            )
            processed_count += 1
        except Exception as e:
            error_msg = f"Error processing class {cls_name}: {str(e)}\n{traceback.format_exc()}"
            errors.append(error_msg)
            log.error(error_msg)

    # === 4. Generate property documentation pages ===
    for prop_qname, prop_uri in prop_map.items():
        if str(prop_uri).startswith(ns):
            generate_property_markdown(
                g, prop_uri, prop_qname, ns, prefix_map,
                docs_dir, global_all_classes,
                ontology_info[list(ontology_info.keys())[0]]["draft"]
                if ontology_info else False,
                global_all_datatypes=global_all_datatypes,
            )

    # === 4b. Generate datatype documentation pages ===
    for dt_qname, dt_uri in datatype_map.items():
        if str(dt_uri).startswith(ns):
            generate_datatype_markdown(
                g, dt_uri, dt_qname, ns, prefix_map,
                docs_dir, global_all_classes,
                ontology_info[list(ontology_info.keys())[0]]["draft"]
                if ontology_info else False,
                global_all_datatypes=global_all_datatypes,
            )

    # === 5. Generate index + pattern overview pages ===
    preferred_prefix = get_preferred_prefix(g)
    home_ont_name = resolve_home_ontology(ontology_info, preferred_prefix)
    index_generated = False
    for ont_name, ont in ontology_info.items():
        log.debug(f"Generating overview for pattern: {ont_name} (preferred prefix: {preferred_prefix})")
        if ont_name.endswith("-reqview"):
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
                          class_to_onts, ontology_info, owl_files)
    except Exception as e:
        error_msg = f"Error updating mkdocs.yml: {str(e)}\n{traceback.format_exc()}"
        errors.append(error_msg)
        log.error(error_msg)

    log.info(f"Finished — processed {processed_count} classes")
    if errors:
        log.error("Errors encountered:")
        for err in errors:
            log.error(err)

    # === 7. Generate ReqView update CSV ===
    try:
        generate_reqview_update_csv(g, local_classes, ns, prefix_map, docs_dir, create_missing)
    except Exception as e:
        error_msg = f"Error generating ReqView update CSV: {str(e)}\n{traceback.format_exc()}"
        errors.append(error_msg)
        log.error(error_msg)

if __name__ == "__main__":
    main()
