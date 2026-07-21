# markdown_generator.py
import os
import logging
import yaml
import traceback
from collections import defaultdict
from rdflib import Graph, URIRef, OWL, RDFS, RDF
from rdflib.namespace import DCTERMS, SKOS, SH
from utils import (
    get_preferred_prefix, get_qname, get_first_literal, get_shacl_name, insert_spaces, class_restrictions,
    iter_annotations, DESC_PROPS, get_definition,
    get_ontology_for_uri, hyperlink_concept, get_url, get_shacl_constraints, get_pattern_name,
    resolve_home_ontology, should_skip_nav_ontology, get_source_ttl_basename, is_pattern_ttl_file,
    get_pattern_modules, get_nav_modules,
)
from diagram_generator import generate_diagram, get_id

log = logging.getLogger("ttl2mkdocs")

def _pattern_page_relpath(ont_name: str, ontology_info: dict) -> str:
    """
    Pattern pages should use the case-sensitive ontology module name (e.g., AreaPattern),
    and live under docs/classes/ so they behave like other UpperCamelCase pages.
    """
    module_name = ontology_info.get(ont_name, {}).get("module_name") or ont_name
    return f"classes/{module_name}.md"

class SafeMkDocsLoader(yaml.SafeLoader):
    """Custom YAML loader to handle MkDocs-specific python/name tags."""
    def ignore_python_name(self, node):
        """Treat python/name tags as strings."""
        return self.construct_scalar(node)

yaml.SafeLoader.add_constructor('tag:yaml.org,2002:python/name:material.extensions.emoji.twemoji', SafeMkDocsLoader.ignore_python_name)
yaml.SafeLoader.add_constructor('tag:yaml.org,2002:python/name:pymdownx.superfences.fence_code_format', SafeMkDocsLoader.ignore_python_name)
yaml.SafeLoader.add_constructor('tag:yaml.org,2002:python/name:material.extensions.emoji.to_svg', SafeMkDocsLoader.ignore_python_name)

def get_specializations(g: Graph, cls: URIRef, global_all_classes: set, ns: str, prefix_map: dict, ns_to_ontology: dict) -> list:
    """Find all subclasses (direct and indirect) of the given class.

    Each entry is (cls_name, desc, ont, uri, is_alignment).
    is_alignment is True when the subclass IRI is outside this ontology's
    master namespace (typical for alignment modules such as
    ConditionVehicleAlignment).
    """
    specializations = []
    visited = set()

    def collect_subclasses(c):
        if c in visited:
            return
        visited.add(c)
        for s in g.subjects(RDFS.subClassOf, c):
            if isinstance(s, URIRef) and s != c:
                cls_name = get_first_literal(g, s, [RDFS.label]) or str(s).split('/')[-1].split('#')[-1]
                ont = get_ontology_for_uri(str(s), ns_to_ontology)
                desc = get_definition(g, s)
                is_alignment = not str(s).startswith(ns)
                specializations.append((cls_name, desc, ont, s, is_alignment))
                collect_subclasses(s)
    collect_subclasses(cls)
    log.debug(f"Specializations for {cls}: {specializations}")
    return sorted(specializations, key=lambda x: x[0].lower())

def get_used_by(g: Graph, cls: URIRef, global_all_classes: set, ns: str, prefix_map: dict, ns_to_ontology: dict) -> list:
    """Find classes and their properties that reference this class via object property restrictions."""
    used_by = []
    for s in g.subjects(RDF.type, OWL.Restriction):
        prop = g.value(s, OWL.onProperty)
        for predicate in [OWL.allValuesFrom, OWL.someValuesFrom, OWL.hasValue]:
            target = g.value(s, predicate)
            if target == cls and prop:
                prop_name = get_qname(prop, ns, prefix_map)
                for cls_sub in g.subjects(RDFS.subClassOf, s):
                    if isinstance(cls_sub, URIRef):
                        cls_name = get_first_literal(g, cls_sub, [RDFS.label]) or str(cls_sub).split('/')[-1].split('#')[-1]
                        ont = get_ontology_for_uri(str(cls_sub), ns_to_ontology)
                        if cls_name in global_all_classes and ont:
                            used_by.append((cls_name, prop_name, ont, cls_sub, prop))
                cls_name = get_first_literal(g, s, [RDFS.label]) or str(s).split('/')[-1].split('#')[-1]
                ont = get_ontology_for_uri(str(s), ns_to_ontology)
                if cls_name in global_all_classes and ont:
                    used_by.append((cls_name, prop_name, ont, s, prop))
    log.debug(f"Used by for {cls}: {used_by}")
    return sorted(used_by, key=lambda x: x[0].lower())

def generate_markdown(g: Graph, cls: URIRef, cls_name: str, global_all_classes: set, ns: str, docs_dir: str, errors: list, prefix_map: dict, ns_to_ontology: dict, class_to_onts: dict, isDraft: bool):
    """Generate Markdown file for a class, including diagram and merged OWL + SHACL formalization."""
    classes_dir = os.path.join(docs_dir, "classes")
    os.makedirs(classes_dir, exist_ok=True)
    filename = os.path.join(classes_dir, f"{cls_name}.md")
    log.debug(f"Writing {filename} for class {cls_name}")

    title = f"# {cls_name}\n\n"
    desc = get_definition(g, cls)
    top_desc = f"{desc}\n\n" if desc else ""
    note = get_first_literal(g, cls, [SKOS.note]) or ""
    note_md = f"NOTE: {note}\n\n" if note else ""
    example = get_first_literal(g, cls, [SKOS.example]) or ""
    example_md = f"EXAMPLE: {example}\n\n" if example else ""

    # Generate diagram (OWL + SHACL already merged inside generate_diagram)
    cls_id = get_id(cls_name.replace(":", "_"))
    try:
        generate_diagram(g, cls, cls_name, cls_id, ns, global_all_classes, {}, "dummy.ttl", errors, prefix_map, "", {})
    except Exception as e:
        error_msg = f"Error generating diagram for {cls_name}: {str(e)}\n{traceback.format_exc()}"
        errors.append(error_msg)
        log.error(error_msg)
        
    # Read SVG content
    svg_file = os.path.join(docs_dir, "diagrams", f"{cls_name}.dot.svg")
    try:
        with open(svg_file, "r", encoding="utf-8") as f:
            svg_lines = f.readlines()
        # Skip first two lines (DOCTYPE and possible comment)
        svg_content = "".join(svg_lines[3:])
        # Markdown would treat "*" inside inlined SVG as emphasis and corrupt
        # labels like "[0..*]" — escape before embedding.
        svg_content = svg_content.replace("*", "&#42;")
        # Indent for tab
        indented_svg = "\n    ".join(svg_content.splitlines()) + "\n"
    except Exception as e:
        error_msg = f"Error reading SVG for {cls_name}: {str(e)}\n{traceback.format_exc()}"
        errors.append(error_msg)
        log.error(error_msg)
        indented_svg = "Error loading diagram."

    diagram_line = f"""
## Diagram

=== "SVG (interactive)"

    {indented_svg}

=== "PNG"

    ![{cls_name} Diagram](../diagrams/{cls_name}.dot.png)

\n\n"""  

    # Specializations section
    specializations = get_specializations(g, cls, global_all_classes, ns, prefix_map, ns_to_ontology)
    specializations_md = ""
    if specializations:
        has_alignment = any(is_alignment for *_, is_alignment in specializations)
        specializations_md += f"## Specializations of {cls_name}\n\n"
        specializations_md += "| Class | Description |\n"
        specializations_md += "|-------|-------------|\n"
        for spec_cls, spec_desc, spec_ont, spec_uri, is_alignment in specializations:
            display_spec = insert_spaces(spec_cls)
            link = get_url(spec_uri, ns, prefix_map, global_all_classes)
            class_cell = f"[{display_spec}]({link})"
            if is_alignment:
                class_cell += "[^alignment]"
            specializations_md += f"| {class_cell} | {spec_desc} |\n"
        specializations_md += "\n"
        if has_alignment:
            specializations_md += (
                "[^alignment]: Specialization asserted by this ontology via alignment; "
                "the class is defined in an external ontology and is not declared as a "
                "specialization of this class there.\n\n"
            )
    else:
        log.debug(f"No specializations found for {cls_name}")
    
    # Formalization section with superclasses and disjoints
    restr_rows = class_restrictions(g, cls, ns, prefix_map, global_all_classes, current_doc_dir="classes")
    superclasses = []
    disjoints = []    
    # Collect direct superclasses
    for super_cls in g.objects(cls, RDFS.subClassOf):
        if isinstance(super_cls, URIRef) and super_cls != OWL.Thing:
            super_name = get_qname(super_cls, ns, prefix_map)
            hyper_super = hyperlink_concept(super_cls, ns, prefix_map, global_all_classes, super_name, current_doc_dir="classes")
            superclasses.append(("subClassOf", hyper_super))

    # Collect disjointWith
    for disjoint_cls in g.objects(cls, OWL.disjointWith):
        if isinstance(disjoint_cls, URIRef):
            disjoint_name = get_qname(disjoint_cls, ns, prefix_map)
            hyper_disjoint = hyperlink_concept(disjoint_cls, ns, prefix_map, global_all_classes, disjoint_name, current_doc_dir="classes")
            disjoints.append(("disjointWith", hyper_disjoint))    
    
    shacl_rows = []
    shacl_data = get_shacl_constraints(g, cls, ns, prefix_map)
    for prop_name, parts in shacl_data.items():
        hyper_prop = hyperlink_concept(prop_name, ns, prefix_map, global_all_classes, current_doc_dir="classes")
        shacl_rows.append((hyper_prop, '; '.join(parts)))

    # Combine with restrictions from class_restrictions
    formalization_rows = sorted(restr_rows + superclasses + disjoints + shacl_rows, key=lambda x: x[0].lower())
    formalization_md = ""
    if formalization_rows:
        formalization_md += f"## Formalization for {cls_name}\n\n"
        formalization_md += "| Property | Constraint |\n"
        formalization_md += "|----------|------------|\n"
        for prop, constr in formalization_rows:
            log.debug(f"Restriction for {cls_name}: ({prop}, '{constr}')")
            formalization_md += f"| {prop} | {constr} |\n"
        formalization_md += "\n"
    
    # Used by section
    used_by = get_used_by(g, cls, global_all_classes, ns, prefix_map, ns_to_ontology)
    used_by_md = ""
    if used_by:
        used_by_md += f"## Used by classes\n\n"
        used_by_md += "| Class | Property |\n"
        used_by_md += "|-------|----------|\n"
        for used_cls, used_prop, used_ont, used_uri, prop_uri in used_by:
            display_used = insert_spaces(used_cls)
            if len(class_to_onts[used_cls]) > 1:
                display_used += f" ({used_ont})"
            elif ':' in used_prop:
                prefix = used_prop.split(':')[0]
                display_used += f" ({prefix})"
            link = get_url(used_uri, ns, prefix_map, global_all_classes)
            hyper_prop = hyperlink_concept(prop_uri, ns, prefix_map, global_all_classes, used_prop, current_doc_dir="classes")
            used_by_md += f"| [{display_used}]({link}) | {hyper_prop} |\n"
        used_by_md += "\n"
    
    # Other annotations
    other_annot_md = ""
    annotations = list(iter_annotations(g, cls, ns, prefix_map))
    if annotations:
        other_annot_md += "## Other annotations\n\n"
        other_annot_md += "| Property | Value |\n"
        other_annot_md += "|----------|-------|\n"
        for pred, val in annotations:
            hyper_pred = hyperlink_concept(pred, ns, prefix_map, global_all_classes, pred, current_doc_dir="classes")
            other_annot_md += f"| {hyper_pred} | {val} |\n"
        other_annot_md += "\n"
    
    content = title + top_desc + note_md + example_md + diagram_line + specializations_md + formalization_md + used_by_md + other_annot_md

    # Write Markdown file
    try:
        with open(filename, "w", encoding="utf-8") as f:
            if isDraft:
                f.write("![Draft for review only](https://isotc204.org/assets/img/draft_for_review.svg)\n\n")
            f.write(content)
        log.info("Generated Markdown at %s", filename)
    except Exception as e:
        error_msg = f"Error writing {filename}: {str(e)}\n{traceback.format_exc()}"
        errors.append(error_msg)
        log.error(error_msg)
        raise

def get_direct_classes_for_ontology(ont_name: str, ontology_info: dict, class_to_onts: defaultdict) -> list:
    """Return only classes directly defined in this ontology (not imported ones)."""
    direct = []
    for cls_name, declaring_onts in class_to_onts.items():
        if ont_name in declaring_onts:          # declared in this file
            # Optional: also ensure it's not ONLY from imports
            direct.append(cls_name)
    return sorted(direct, key=str.lower)


def _append_concepts_section(
    content: str,
    section_title: str,
    classes: list,
    properties: list,
    class_link_prefix: str = "classes/",
    prop_link_prefix: str = "properties/",
) -> str:
    """Append markdown lists of direct classes and properties."""
    if not classes and not properties:
        return content

    content += f"\n## {section_title}\n\n"
    if classes:
        content += "### Classes\n\n"
        for cls_name in classes:
            if cls_name == "ITSThing":
                continue
            display_cls = insert_spaces(cls_name)
            content += f"- [{display_cls}]({class_link_prefix}{cls_name}.md)\n"
        content += "\n"
    if properties:
        content += "### Properties\n\n"
        for prop_qname in properties:
            content += f"- [{prop_qname}]({prop_link_prefix}{prop_qname}.md)\n"
        content += "\n"
    return content


def update_mkdocs_nav(mkdocs_path: str, 
                      global_patterns: dict, 
                      global_all_classes: set, 
                      errors: list, 
                      class_to_onts: defaultdict, 
                      ontology_info: dict, 
                      input_files: list):
    """Update mkdocs.yml navigation so each pattern shows overview/classes/properties."""
    try:
        with open(mkdocs_path, 'r', encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except Exception as e:
        error_msg = f"Error reading mkdocs.yml: {str(e)}\n{traceback.format_exc()}"
        errors.append(error_msg)
        log.error(error_msg)
        raise

    new_nav = [{"Home": "index.md"}]
    pattern_modules = get_pattern_modules(ontology_info)

    if not pattern_modules:
        # Single main TTL (no *-pattern.ttl): flat nav — no Pattern/Overview wrapper
        class_items = []
        prop_items = []
        for ont_name in get_nav_modules(ontology_info):
            for cls_name in get_direct_classes_for_ontology(ont_name, ontology_info, class_to_onts):
                if cls_name == "ITSThing":
                    continue
                class_items.append({insert_spaces(cls_name): f"classes/{cls_name}.md"})
            for prop_qname in sorted(ontology_info.get(ont_name, {}).get("properties", []) or [], key=str.lower):
                prop_items.append({prop_qname: f"properties/{prop_qname}.md"})
        if class_items:
            new_nav.append({"Classes": class_items})
        if prop_items:
            new_nav.append({"Properties": prop_items})
    else:
        for ont_name in get_nav_modules(ontology_info):
            direct_classes = get_direct_classes_for_ontology(ont_name, ontology_info, class_to_onts)
            display_ont = f"{insert_spaces(ont_name)} Pattern"
            ont_nav = [{f"{display_ont} Overview": _pattern_page_relpath(ont_name, ontology_info)}]

            class_items = []
            for cls_name in direct_classes:
                if cls_name == "ITSThing":
                    continue
                class_items.append({insert_spaces(cls_name): f"classes/{cls_name}.md"})
            if class_items:
                ont_nav.append({"Classes": class_items})

            prop_items = []
            for prop_qname in sorted(ontology_info.get(ont_name, {}).get("properties", []) or [], key=str.lower):
                prop_items.append({prop_qname: f"properties/{prop_qname}.md"})
            if prop_items:
                ont_nav.append({"Properties": prop_items})

            new_nav.append({display_ont: ont_nav})

    config["nav"] = new_nav

    try:
        with open(mkdocs_path, 'w', encoding="utf-8") as f:
            yaml.safe_dump(config, f, sort_keys=False, allow_unicode=True, default_flow_style=False)
    except Exception as e:
        error_msg = f"Error writing mkdocs.yml: {str(e)}\n{traceback.format_exc()}"
        errors.append(error_msg)
        log.error(error_msg)
        raise

def generate_index(g: Graph, ont_name: str, ns: str, prefix_map: dict, ont: dict, docs_dir: str, ontology_info: dict, errors: list, class_to_onts: defaultdict, isDraft: bool):
    """Generate index.md with one section per pattern."""
    index_path = os.path.join(docs_dir, "index.md")
    index_content = f"# {ont['title']}\n\n"
    preferred_prefix = get_preferred_prefix(g)
    home_ont_name = resolve_home_ontology(ontology_info, preferred_prefix) or ont_name
    if isDraft:
        index_content += "![Draft for review only](https://isotc204.org/assets/img/draft_for_review.svg)\n\n"
    pattern_modules = get_pattern_modules(ontology_info)
    if pattern_modules:
        if ont.get("description"):
            index_content += ont["description"] + "\n\n"
        index_content += f"The {ont['title']} consists of the following patterns:\n\n"
        for pattern_name in pattern_modules:
            display = insert_spaces(pattern_name)
            link = _pattern_page_relpath(pattern_name, ontology_info)
            index_content += f"- [{display}]({link})\n"

        # Non-pattern modules (e.g. core.ttl, or the main ontology TTL) with direct concepts
        for module_name in sorted(ontology_info.keys(), key=str.lower):
            if module_name.endswith("-reqview") or is_pattern_ttl_file(ontology_info[module_name]):
                continue
            module = ontology_info[module_name]
            direct_classes = get_direct_classes_for_ontology(module_name, ontology_info, class_to_onts)
            direct_props = sorted(module.get("properties") or [], key=str.lower)
            if not direct_classes and not direct_props:
                continue
            if module_name == home_ont_name:
                section_title = "Core concepts"
            else:
                section_title = module.get("title") or insert_spaces(module_name)
            index_content = _append_concepts_section(
                index_content, section_title, direct_classes, direct_props
            )
    else:
        single_name = home_ont_name if home_ont_name in ontology_info else sorted(ontology_info.keys(), key=str.lower)[0]
        single_ont = ontology_info[single_name]
        if single_ont.get("description"):
            index_content += single_ont["description"] + "\n\n"
        if single_ont.get("imports"):
            index_content += "This ontology imports the following files:\n\n"
            for imp_iri in single_ont["imports"]:
                index_content += f"- [{imp_iri}]({imp_iri})\n"
            index_content += "\n"
        direct_classes = get_direct_classes_for_ontology(single_name, ontology_info, class_to_onts)
        direct_props = sorted(single_ont.get("properties") or [], key=str.lower)
        index_content = _append_concepts_section(
            index_content, "Ontology concepts", direct_classes, direct_props
        )

    home_ont = ontology_info.get(home_ont_name, ont)
    filename_ttl = get_source_ttl_basename(home_ont_name, home_ont)
    index_content += f"\nThe formal definition of this ontology is available in [TURTLE Syntax]({filename_ttl}).\n"

    # Write file
    try:
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(index_content)
        log.info("Generated updated index.md with per-pattern class lists")
    except Exception as e:
        error_msg = f"Error writing index.md: {str(e)}\n{traceback.format_exc()}"
        errors.append(error_msg)
        log.error(error_msg)
        raise

def generate_pattern_markdown(
    g: Graph,
    ont_name: str,
    ns: str,
    prefix_map: dict,
    ont: dict,
    docs_dir: str,
    class_to_onts: defaultdict,
    ontology_info: dict,
    *,
    for_index: bool = False,
):
    """Generate pattern overview markdown (under docs/classes/ unless for_index)."""
    title = "# " + ont["title"] + "\n\n"
    desc = ont["description"] or ""
    top_desc = f"{desc}\n\n" if desc else ""

    imports_md = ""
    if ont.get("imports"):
        label = "This ontology imports" if for_index else "This pattern imports"
        imports_md = f"{label} the following files:\n\n"
        for imp_iri in ont["imports"]:
            imports_md += f"- [{imp_iri}]({imp_iri})\n"
        imports_md += "\n"

    direct_classes = get_direct_classes_for_ontology(ont_name, ontology_info, class_to_onts)
    direct_props = sorted(ont.get("properties") or [], key=str.lower)

    if for_index:
        body = ""
        if direct_classes or direct_props:
            body = _append_concepts_section(body, "Ontology concepts", direct_classes, direct_props)
        elif not direct_classes:
            body = "This ontology does not declare any classes or properties.\n\n"
    else:
        members_md = "This pattern consists of the following classes:\n\n"
        i = 0
        for cls_name in direct_classes:
            if cls_name == "ITSThing":
                continue
            display_cls = insert_spaces(cls_name)
            members_md += f"- [{display_cls}]({cls_name}.md)\n"
            i += 1
        if i == 0:
            members_md = "This pattern does not contain any classes.\n"

        props_md = ""
        if direct_props:
            props_md = "This module defines the following properties:\n\n"
            for prop_qname in direct_props:
                props_md += f"- [{prop_qname}](../properties/{prop_qname}.md)\n"
            props_md += "\n"
        body = members_md + props_md

    owl_ttl = get_source_ttl_basename(ont_name, ont)
    shacl_ttl = get_shacl_name(ont_name) + ".ttl"
    shacl_path = os.path.join(docs_dir, shacl_ttl)
    ttl_prefix = "" if for_index else "../"
    if os.path.exists(shacl_path):
        formal = (
            f"\nThe formal definition of this pattern is available in TURTLE Syntax in two files, "
            f"the [core semantics]({ttl_prefix}{owl_ttl}) and the SHACL "
            f"[restrictions]({ttl_prefix}{shacl_ttl}).\n"
        )
    else:
        label = "ontology" if for_index else "pattern"
        formal = f"\nThe formal definition of this {label} is available in [TURTLE Syntax]({ttl_prefix}{owl_ttl}).\n"

    if for_index:
        return top_desc + imports_md + body + formal
    return title + top_desc + imports_md + body + formal

def generate_pattern_markdown_file(g: Graph, ont_name: str, ns: str, prefix_map: dict, ont: dict, docs_dir: str, class_to_onts: defaultdict, ontology_info: dict):
    """Generate the per-pattern Markdown page, now including imported patterns."""
    content = generate_pattern_markdown(g, ont_name, ns, prefix_map, ont, docs_dir, class_to_onts, ontology_info)
    classes_dir = os.path.join(docs_dir, "classes")
    os.makedirs(classes_dir, exist_ok=True)
    module_name = ontology_info.get(ont_name, {}).get("module_name") or ont_name
    filename = os.path.join(classes_dir, f"{module_name}.md")
    with open(filename, "w", encoding="utf-8") as f:
        if ontology_info[ont_name].get("draft"):
            f.write("![Draft for review only](https://isotc204.org/assets/img/draft_for_review.svg)\n\n")
        f.write(content)
    log.info("Generated pattern Markdown at %s", filename)

def generate_property_markdown(g: Graph, prop_uri: URIRef, prop_name: str, 
                               ns: str, prefix_map: dict, docs_dir: str, 
                               global_all_classes: set, isDraft: bool):
    """Generate a dedicated Markdown page for a property."""
    prop_dir = os.path.join(docs_dir, "properties")
    os.makedirs(prop_dir, exist_ok=True)

    filename = os.path.join(prop_dir, f"{prop_name}.md")

    title = f"# {prop_name}\n\n"
    desc = get_definition(g, prop_uri)

    # Domain & Range
    domain = []
    for d in g.objects(prop_uri, RDFS.domain):
        domain.append(hyperlink_concept(d, ns, prefix_map, global_all_classes, current_doc_dir="properties"))
    range_ = []
    for r in g.objects(prop_uri, RDFS.range):
        range_.append(hyperlink_concept(r, ns, prefix_map, global_all_classes, current_doc_dir="properties"))
        log.info(f"Range for {prop_name} with {ns} and {r} : {range_}")

    # SHACL usage
    used_in = []
    for shape in g.subjects(SH.targetClass, None):
        for pshape in g.objects(shape, SH.property):
            if g.value(pshape, SH.path) == prop_uri:
                target_cls = g.value(shape, SH.targetClass)
                if target_cls:
                        used_in.append(hyperlink_concept(target_cls, ns, prefix_map, global_all_classes, current_doc_dir="properties"))

    content = title + (desc + "\n\n" if desc else "")
    
    if domain:
        content += f"**Domain**: {', '.join(domain)}\n\n"
    if range_:
        content += f"**Range**: {', '.join(range_)}\n\n"

    if used_in:
        content += "## Used in classes\n\n"
        content += "| Class |\n|-------|\n"
        for cls_link in used_in:
            content += f"| {cls_link} |\n"
        content += "\n"

    content += f"**IRI**: `{str(prop_uri)}`\n"

    with open(filename, "w", encoding="utf-8") as f:
        if isDraft:
            f.write("![Draft for review only](https://isotc204.org/assets/img/draft_for_review.svg)\n\n")
        f.write(content)

    log.info(f"Generated property page: {prop_name}.md")