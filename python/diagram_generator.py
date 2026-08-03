# diagram_generator.py
import os
import html
import logging
import traceback
from rdflib import Graph, URIRef, Literal
from rdflib.namespace import RDF, RDFS, OWL, SH
from graphviz import Digraph # type: ignore
from collections import defaultdict

from utils import (
    get_qname, get_id, get_property_info, is_refined_property,
    collect_list, get_class_expression_str, get_ontology_for_uri,
    insert_spaces, get_leaf_classes, collect_oneOf, get_url,
    get_shacl_diagram_constraints, prefix_to_uml_namespace
)

# Configure logging
log = logging.getLogger("ttl2mkdocs")


def _format_edge_label(label: str) -> str:
    """Render an association label as padded HTML so text does not sit on the stroke.

    Thin letters (i, l, …) otherwise often collide with the edge line. A white
    background plus cell padding keeps a readable gap around the name.
    """
    rows = "".join(
        f'<TR><TD ALIGN="CENTER">{html.escape(line)}</TD></TR>'
        for line in str(label).split("\n")
    )
    return (
        f'<<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="0" '
        f'CELLPADDING="4" BGCOLOR="white">{rows}</TABLE>>'
    )

def get_target_info(g: Graph, expr, cls_name: str, ns: str, prefix_map: dict) -> tuple:
    """Get target information for a property's range, handling complex expressions."""
    if not expr:
        return None, False, None, None, False, None
    if isinstance(expr, URIRef):
        target_qname = get_qname(expr, ns, prefix_map)
        if target_qname == 'ITSThing':
            return None, False, None, None, False, None
        target_id = get_id(target_qname.replace(":", "_"))
        reflexive = target_qname == cls_name
        is_complex = False
        return target_id, is_complex, None, target_qname, reflexive, expr
    else:  # BNode, complex expression
        target_id = str(expr).replace(":", "_").replace("/", "_").replace("#", "_").replace("_:", "bnode_")
        target_qname = get_class_expression_str(g, expr, ns, prefix_map)
        reflexive = False
        is_complex = True
        return target_id, is_complex, None, target_qname, reflexive, expr

def _to_uml_multiplicity(label_parts: list) -> str | None:
    """Convert verbose SHACL/OWL constraints into compact UML multiplicity.
    e.g. ['exactly 1'] → '1'
         ['min 0', 'max *'] → '0..*'
         ['min 1', 'max 1'] → '1'
    """
    if not label_parts:
        return None

    parts = [str(p).strip() for p in label_parts]

    for part in parts:
        if part.startswith("exactly "):
            n = part[8:].strip()
            return "1" if n == "1" else n

        # Combined min + max
        if any(p.startswith("min ") for p in parts) and any(p.startswith("max ") for p in parts):
            min_val = max_val = None
            for p in parts:
                if p.startswith("min "):
                    min_val = p[4:].strip()
                elif p.startswith("max "):
                    max_val = p[4:].strip()
            if min_val is not None and max_val is not None:
                if min_val == max_val:
                    return "1" if min_val == "1" else min_val
                max_str = max_val if max_val not in ("unbounded", "*") else "*"
                return f"{min_val}..{max_str}"

        if part.startswith("min "):
            min_val = part[4:].strip()
            return f"{min_val}..*"

        if part.startswith("max "):
            max_val = part[4:].strip()
            return f"0..{max_val if max_val not in ('unbounded', '*') else '*'}"

    return None

def add_class_expression_node(graph, g: Graph, expr, ns: str, prefix_map: dict, global_all_classes: set, ns_to_ontology: dict, abstract_map: dict, created: set, is_superclass: bool = False, in_associated_cluster: bool = False, enum_members: list = None, enum_name: str = None) -> tuple:
    """Recursively add nodes for class expressions, returning (node_id, label)."""
    if isinstance(expr, URIRef):
        qname = get_qname(expr, ns, prefix_map)
        node_id = get_id(qname.replace(":", "_"))
        if node_id in created:
            return node_id, qname
        created.add(node_id)
        local = qname.split(":")[-1]
        target_ont = get_ontology_for_uri(str(expr), ns_to_ontology)
        url = get_url(expr, ns, prefix_map, global_all_classes, False)
        label = qname
        graph.node(
            node_id,
            label=f'<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="0" CELLPADDING="1"><TR><TD BGCOLOR="lightgray" ALIGN="CENTER">{label}</TD></TR></TABLE>>',
            URL=url,
            margin="0"
        )
        log.debug("Added node %s: %s (superclass=%s, in_associated_cluster=%s)", node_id, qname, is_superclass, in_associated_cluster)
        return node_id, qname
    else:  # BNode
        node_id = str(expr).replace(":", "_").replace("/", "_").replace("#", "_").replace("_:", "bnode_")
        if node_id in created:
            return node_id, get_class_expression_str(g, expr, ns, prefix_map)
        created.add(node_id)
        expr_str = get_class_expression_str(g, expr, ns, prefix_map)

        # Handle unionOf, intersectionOf, complementOf, oneOf (unchanged from your original)
        union_col = g.value(expr, OWL.unionOf)
        if union_col and union_col != RDF.nil:
            members = collect_list(g, union_col)
            stereo = "unionOf"
            graph.node(node_id, f'<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="0" CELLPADDING="1" BGCOLOR="lightyellow"><TR><TD ALIGN="CENTER">«{stereo}»</TD></TR></TABLE>>', margin="0")
            for member in sorted(members, key=str):
                member_id, _ = add_class_expression_node(graph, g, member, ns, prefix_map, global_all_classes, ns_to_ontology, abstract_map, created, is_superclass=False, in_associated_cluster=in_associated_cluster)
                graph.edge(node_id, member_id, style="dotted", label="member", arrowhead="normal")
            return node_id, ""

        inter_col = g.value(expr, OWL.intersectionOf)
        if inter_col and inter_col != RDF.nil:
            members = collect_list(g, inter_col)
            stereo = "intersectionOf"
            graph.node(node_id, f'<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="0" CELLPADDING="1" BGCOLOR="lightyellow"><TR><TD ALIGN="CENTER">«{stereo}»</TD></TR></TABLE>>', margin="0")
            for member in sorted(members, key=str):
                member_id, _ = add_class_expression_node(graph, g, member, ns, prefix_map, global_all_classes, ns_to_ontology, abstract_map, created, is_superclass=False, in_associated_cluster=in_associated_cluster)
                graph.edge(node_id, member_id, style="dotted", label="member", arrowhead="normal")
            return node_id, ""

        complement = g.value(expr, OWL.complementOf)
        if complement:
            stereo = "not"
            graph.node(node_id, f'<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="0" CELLPADDING="1" BGCOLOR="lightyellow"><TR><TD ALIGN="CENTER">«{stereo}»</TD></TR></TABLE>>', margin="0")
            comp_id, _ = add_class_expression_node(graph, g, complement, ns, prefix_map, global_all_classes, ns_to_ontology, abstract_map, created, is_superclass=False, in_associated_cluster=in_associated_cluster)
            graph.edge(node_id, comp_id, style="dotted", label="of", arrowhead="normal")
            return node_id, ""

        oneOf_members = collect_oneOf(g, expr)
        if oneOf_members:
            stereo = "Enum"
            member_str = '<BR/>'.join([f"+ {get_qname(m, ns, prefix_map)}" for m in sorted(oneOf_members, key=str)])
            graph.node(node_id, f'<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="0" CELLPADDING="1"><TR><TD BGCOLOR="lightgray" ALIGN="CENTER">«{stereo}»<BR/>{enum_name or expr_str}</TD></TR><TR><TD ALIGN="LEFT">{member_str}</TD></TR></TABLE>>', margin="0")
            return node_id, enum_name or ""

        # Default for other expressions
        graph.node(node_id, f'<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="0" CELLPADDING="1" BGCOLOR="lightyellow"><TR><TD ALIGN="CENTER">{expr_str}</TD></TR></TABLE>>', margin="0")
        return node_id, expr_str


def generate_diagram(g: Graph, cls: URIRef, cls_name: str, cls_id: str, ns: str, global_all_classes: set, abstract_map: dict, ofn_path: str, errors: list, prefix_map: dict, ontology_name: str, ns_to_ontology: dict):
    """Generate a DOT file for a given class, producing an ODM-like diagram.
    OWL restrictions and SHACL constraints are merged for both datatype attributes and object associations."""
    
    # Ensure output directory exists
    diagrams_dir = os.path.join(os.path.dirname(ofn_path), "docs/diagrams")
    if not os.path.exists(diagrams_dir):
        os.makedirs(diagrams_dir)
        log.info(f"Created diagrams directory: {diagrams_dir}")

    cls_filename = cls_name

    # Initialize Digraph with ODM-like styling
    dot = Digraph(
        comment=f"Diagram for {cls_name}",
        format="svg",
        graph_attr={"overlap": "false", "splines": "true", "rankdir": "TB"},
        node_attr={"shape": "none", "fontsize": "12", "fontname": "Arial", "margin": "0"},
        edge_attr={"fontsize": "11", "fontname": "Arial"}
    )
    dot.engine = 'dot'

    # Track combined properties to merge restrictions
    combined = defaultdict(dict)

    # Collect superclasses URIs
    super_uris = set()
    for sup in g.objects(cls, RDFS.subClassOf):
        if isinstance(sup, URIRef) and sup != OWL.Thing:
            super_uris.add(sup)
        elif (sup, RDF.type, OWL.Class) in g and g.value(sup, OWL.unionOf):
            members = collect_list(g, g.value(sup, OWL.unionOf))
            for m in members:
                if isinstance(m, URIRef):
                    super_uris.add(m)

# === SUPERCLASSES - PLACE THEM AT THE VERY TOP ===
    created_super = set()
    super_ids = []

    with dot.subgraph() as top_group:
        top_group.attr(rank='source')        # This forces all superclasses to the top
        
        for sup_uri in sorted(super_uris, key=lambda u: get_qname(u, ns, prefix_map).lower()):
            sup_id, _ = add_class_expression_node(
                top_group, g, sup_uri, ns, prefix_map, global_all_classes,
                ns_to_ontology, abstract_map, created_super, is_superclass=True
            )
            super_ids.append(sup_id)

    # === MAIN CLASS NODE WITH DATATYPE PROPERTIES ===
    needs_unique_lang_note = False
    with dot.subgraph() as main_group:
        main_group.attr(rank='max')
        data_props = defaultdict(list)   # datatype properties

        # ------------------- 1. OWL DatatypeProperty restrictions -------------------
        for restriction in g.objects(cls, RDFS.subClassOf):
            if (restriction, RDF.type, OWL.Restriction) in g:
                prop = g.value(restriction, OWL.onProperty)
                if not prop:
                    continue
                prop_name, is_inverse, base_prop = get_property_info(g, prop, ns, prefix_map)
                if base_prop and (base_prop, RDF.type, OWL.DatatypeProperty) in g:
                    is_refined = is_refined_property(g, cls, base_prop, restriction)
                    style = "dashed" if is_refined else "solid"
                    label_parts = []

                    # Collect cardinality constraints
                    card = g.value(restriction, OWL.cardinality)
                    min_card = g.value(restriction, OWL.minCardinality)
                    max_card = g.value(restriction, OWL.maxCardinality)
                    if card:
                        label_parts.append(f"exactly {card}")
                    if min_card:
                        label_parts.append(f"min {min_card}")
                    if max_card:
                        label_parts.append(f"max {max_card}")

                    # Collect value constraints and extract the datatype
                    datatype = None
                    all_values_from = g.value(restriction, OWL.allValuesFrom)
                    if all_values_from:
                        avf_qname = get_qname(all_values_from, ns, prefix_map)
                        label_parts.append("only")
                        datatype = avf_qname

                    some_values_from = g.value(restriction, OWL.someValuesFrom)
                    if some_values_from:
                        svf_qname = get_qname(some_values_from, ns, prefix_map)
                        label_parts.append("some")
                        datatype = svf_qname

                    has_value = g.value(restriction, OWL.hasValue)
                    if has_value:
                        hv_str = f"'{has_value}'" if isinstance(has_value, Literal) else get_qname(has_value, ns, prefix_map)
                        label_parts.append(f"value {hv_str}")

                    # OWL 2 datatype restrictions (and common mis-encoding via onClass)
                    on_data_range = g.value(restriction, OWL.onDataRange)
                    if on_data_range and not datatype:
                        datatype = get_qname(on_data_range, ns, prefix_map)
                    on_class = g.value(restriction, OWL.onClass)
                    if on_class and not datatype:
                        datatype = get_qname(on_class, ns, prefix_map)
                    if not datatype:
                        prop_range = g.value(base_prop, RDFS.range)
                        if prop_range:
                            datatype = get_qname(prop_range, ns, prefix_map)

                    key = prop_name
                    if key not in data_props:
                        data_props[key] = {'label_parts': [], 'style': style, 'datatype': datatype}
                    else:
                        data_props[key]['style'] = "dashed" if is_refined else data_props[key]['style']
                    data_props[key]['label_parts'].extend(label_parts)
                    if datatype:
                        data_props[key]['datatype'] = datatype   # keep the last seen datatype

        # ------------------- 2. SHACL constraints for datatype properties -------------------
        shacl_data = get_shacl_diagram_constraints(g, cls, ns, prefix_map)
        for prop_name, parts in shacl_data.items():
            is_datatype = True
            prop_uri = None
            for p in g.subjects(RDF.type, OWL.ObjectProperty):
                if get_qname(p, ns, prefix_map) == prop_name:
                    is_datatype = False
                    prop_uri = p
                    break

            # Open-range object properties (no sh:class / rdfs:range): show in the
            # attribute compartment instead of drawing an owl:Thing association.
            if not is_datatype and prop_uri is not None:
                has_sh_range = 'range' in parts or 'datatype' in parts
                has_rdfs_range = g.value(prop_uri, RDFS.range) is not None
                if not has_sh_range and not has_rdfs_range:
                    is_datatype = True

            if is_datatype:
                if prop_name not in data_props:
                    data_props[prop_name] = {'label_parts': [], 'style': 'solid', 'datatype': None}
                data = shacl_data[prop_name]
                if 'datatype' in data:
                    data_props[prop_name]['datatype'] = data['datatype']
                if 'multiplicity' in data:
                    data_props[prop_name]['multiplicity'] = data['multiplicity']
                if data.get('uniqueLang'):
                    data_props[prop_name]['uniqueLang'] = True


        # Build attribute rows — UML style
        sorted_props = sorted(data_props.items(), key=lambda x: x[0].lower())
        attr_rows = ""
        for prop_name, data in sorted_props:
            mult = data.get('multiplicity')
            datatype = data.get('datatype')
            style = data.get('style', 'solid')
            attr_label = f"{prop_name} : {datatype}" if datatype else prop_name

            if mult:
                attr_label += f" [{mult}]"

            if data.get('uniqueLang'):
                attr_label += "¹"
                needs_unique_lang_note = True

            if style == "dashed":
                attr_label = f"redefines {attr_label}"

            attr_rows += f"<TR><TD ALIGN=\"LEFT\">{attr_label}</TD></TR>"

        # Main class node
        url = f"../{cls_name}"
        dot.node(
            cls_id,
            label=f'<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="0" CELLPADDING="1"><TR><TD BGCOLOR="lightgray" ALIGN="CENTER">{cls_name}</TD></TR>{attr_rows}</TABLE>>',
            URL=url,
            margin="0"
        )
        log.debug("Added main node %s: %s", cls_id, cls_name)

    # === OBJECT PROPERTIES (OWL + SHACL) ===
    associated_uris = set()

    # ------------------- 1. OWL ObjectProperty restrictions -------------------
    for restriction in g.objects(cls, RDFS.subClassOf):
        if (restriction, RDF.type, OWL.Restriction) in g:
            prop = g.value(restriction, OWL.onProperty)
            if not prop:
                continue
            prop_name, is_inverse, base_prop = get_property_info(g, prop, ns, prefix_map)
            # Prefer an explicit ObjectProperty type; if the declaring ontology
            # failed to load, still treat class-valued restrictions as associations
            # unless the property is explicitly a DatatypeProperty.
            is_object = bool(base_prop) and (base_prop, RDF.type, OWL.ObjectProperty) in g
            if base_prop and not is_object and (base_prop, RDF.type, OWL.DatatypeProperty) not in g:
                if (g.value(restriction, OWL.onClass)
                        or g.value(restriction, OWL.allValuesFrom)
                        or g.value(restriction, OWL.someValuesFrom)) and not g.value(restriction, OWL.onDataRange):
                    is_object = True
            if is_object:
                is_refined = is_refined_property(g, cls, base_prop, restriction)
                style = "dashed" if is_refined else "solid"
                label_parts = []
                target_expr = None
                reflexive = False

                on_class = g.value(restriction, OWL.onClass)
                qualified_card = g.value(restriction, OWL.qualifiedCardinality)
                min_qualified_card = g.value(restriction, OWL.minQualifiedCardinality)
                max_qualified_card = g.value(restriction, OWL.maxQualifiedCardinality)
                if qualified_card:
                    label_parts.append(f"exactly {qualified_card}")
                if min_qualified_card:
                    label_parts.append(f"min {min_qualified_card}")
                if max_qualified_card:
                    label_parts.append(f"max {max_qualified_card}")
                if on_class:
                    target_expr = on_class

                all_values_from = g.value(restriction, OWL.allValuesFrom)
                if all_values_from:
                    label_parts.append("only")
                    target_expr = all_values_from

                some_values_from = g.value(restriction, OWL.someValuesFrom)
                if some_values_from:
                    label_parts.append("some")
                    target_expr = some_values_from

                card = g.value(restriction, OWL.cardinality)
                min_card = g.value(restriction, OWL.minCardinality)
                max_card = g.value(restriction, OWL.maxCardinality)
                if card:
                    label_parts.append(f"exactly {card}")
                if min_card:
                    label_parts.append(f"min {min_card}")
                if max_card:
                    label_parts.append(f"max {max_card}")

                if label_parts and not target_expr:
                    target_expr = OWL.Thing

                if target_expr:
                    if not label_parts and target_expr == OWL.Thing:
                        # Cardinality-only with no range is not useful on the diagram
                        continue
                    if isinstance(target_expr, URIRef):
                        associated_uris.add(target_expr)
                    else:
                        leaves = get_leaf_classes(g, target_expr, ns, prefix_map)
                        for leaf in leaves:
                            if isinstance(leaf, URIRef):
                                associated_uris.add(leaf)

                    oneOf_members = collect_oneOf(g, target_expr)
                    enum_name = None
                    if oneOf_members:
                        prop_local = prop_name.split(':')[-1]
                        enum_name = f"{prop_local[0].upper()}{prop_local[1:]}Enum"

                    target_id, _, _, target_qname, reflexive, _ = get_target_info(g, target_expr, cls_name, ns, prefix_map)
                    key = (prop_name, target_id)
                    if key not in combined:
                        combined[key] = {
                            'label_parts': [],
                            'multiplicity': None,
                            'style': style,
                            'prop_name': prop_name,
                            'target_expr': target_expr,
                            'reflexive': reflexive,
                            'target_qname': target_qname,
                            'is_inverse': is_inverse,
                            'enum_members': oneOf_members,
                            'enum_name': enum_name
                        }
                    combined[key].setdefault('label_parts', [])
                    combined[key].setdefault('multiplicity', None)
                    combined[key]['label_parts'].extend(label_parts)
                    combined[key]['style'] = "dashed" if is_refined else combined[key]['style']

    # ------------------- 2. SHACL constraints for object properties (MERGE POINT) -------------------
    shacl_data = get_shacl_diagram_constraints(g, cls, ns, prefix_map)
    for prop_name, parts in shacl_data.items():
        # Skip if already handled as datatype
        if prop_name in data_props:
            continue

        # Find if it's an ObjectProperty
        prop_uri = None
        for p in g.subjects(RDF.type, OWL.ObjectProperty):
            if get_qname(p, ns, prefix_map) == prop_name:
                prop_uri = p
                break

        if prop_uri:
            # Target: sh:class, else non-literal sh:datatype (mis-encoded class),
            # else rdfs:range / schema:rangeIncludes. Avoid bare owl:Thing when
            # the property already declares a range.
            target_expr = None
            for shape in g.subjects(SH.targetClass, cls):
                for prop_shape in g.objects(shape, SH.property):
                    if g.value(prop_shape, SH.path) != prop_uri:
                        continue
                    sh_class = g.value(prop_shape, SH['class'])
                    if sh_class:
                        target_expr = sh_class
                        break
                    sh_datatype = g.value(prop_shape, SH.datatype)
                    if sh_datatype and isinstance(sh_datatype, URIRef):
                        dt = str(sh_datatype)
                        if not (dt.startswith("http://www.w3.org/2001/XMLSchema#")
                                or dt.endswith("langString")
                                or dt.endswith("#string")):
                            target_expr = sh_datatype
                            break
                if target_expr:
                    break
            if target_expr is None:
                target_expr = (
                    g.value(prop_uri, RDFS.range)
                    or g.value(prop_uri, URIRef("http://schema.org/rangeIncludes"))
                    or OWL.Thing
                )

            # Collect associated URIs
            if isinstance(target_expr, URIRef):
                associated_uris.add(target_expr)
            else:
                leaves = get_leaf_classes(g, target_expr, ns, prefix_map)
                for leaf in leaves:
                    if isinstance(leaf, URIRef):
                        associated_uris.add(leaf)

            oneOf_members = collect_oneOf(g, target_expr)
            enum_name = None
            if oneOf_members:
                prop_local = prop_name.split(':')[-1]
                enum_name = f"{prop_local[0].upper()}{prop_local[1:]}Enum"

            target_id, _, _, target_qname, reflexive, _ = get_target_info(g, target_expr, cls_name, ns, prefix_map)
            key = (prop_name, target_id)
            if key not in combined:
                combined[key] = {
                    'multiplicity': None,
                    'style': 'solid',
                    'prop_name': prop_name,
                    'target_expr': target_expr,
                    'reflexive': reflexive,
                    'target_qname': target_qname,
                    'is_inverse': False,
                    'enum_members': oneOf_members,
                    'enum_name': enum_name
                }
            if prop_name in shacl_data and 'multiplicity' in shacl_data[prop_name]:
                combined[key]['multiplicity'] = shacl_data[prop_name]['multiplicity']

            log.debug("Merged SHACL object property %s: constraints=%s (range skipped)",
                     prop_name, combined[key].get('multiplicity'))

    # Remove self and supers from associated
    associated_uris -= {cls}
    associated_uris -= super_uris

    # Add associated nodes (unchanged)
    assoc_nodes = []
    created_complex = set()
    with dot.subgraph(name='cluster_associated') as associated_cluster:
        associated_cluster.attr(style='invis', label='')
        associated_cluster.node('Invis', label='<<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="0" CELLPADDING="1"><TR><TD></TD></TR></TABLE>>', style='invis', margin="0")
        for assoc_uri in sorted(associated_uris, key=lambda u: get_qname(u, ns, prefix_map).lower()):
            assoc_id, _ = add_class_expression_node(associated_cluster, g, assoc_uri, ns, prefix_map, global_all_classes, ns_to_ontology, abstract_map, created_complex, is_superclass=False, in_associated_cluster=True)
            assoc_nodes.append(assoc_id)

    # Add generalization edges for superclasses
    for sup_id in super_ids:
        dot.edge(cls_id, sup_id, arrowhead="onormal", style="solid")

    # Add invisible edges for layout
    if assoc_nodes:
        dot.edge(cls_id, 'Invis', style="invis")
        prev = 'Invis'
        for assoc_id in assoc_nodes:
            dot.edge(prev, assoc_id, style="invis")
            prev = assoc_id

    # Add object property edges (unchanged)
    created_complex = set()  # reset for targets
    super_id_set = set(super_ids)
    for key, data in combined.items():
        prop_name = data['prop_name']
        style = data.get('style', 'solid')
        label_parts = data.get('label_parts', [])   # from OWL mostly now
        reflexive = data['reflexive']
        target_expr = data['target_expr']
        is_inverse = data['is_inverse']
        enum_members = data.get('enum_members', [])
        enum_name = data.get('enum_name')
        target_id, _ = add_class_expression_node(dot, g, target_expr, ns, prefix_map, global_all_classes, ns_to_ontology, abstract_map, created_complex, is_superclass=False, in_associated_cluster=True)
        uml_mult = _to_uml_multiplicity(label_parts)
        if not uml_mult and 'multiplicity' in data:
            uml_mult = data['multiplicity']
        if uml_mult:
            label = f"{prop_name}\n{uml_mult}"
        else:
            label = prop_name

        if style == "dashed":
            label = f"redefines\n{label}"
        label = _format_edge_label(label)

        target_id, _ = add_class_expression_node(dot, g, data['target_expr'], ns, prefix_map, 
                                                 global_all_classes, ns_to_ontology, abstract_map, 
                                                 created_complex, is_superclass=False, in_associated_cluster=True)

        source_id = cls_id if not is_inverse else target_id
        dest_id = target_id if not is_inverse else cls_id
        arrowhead = "normal" if not is_inverse else "inv"
        if reflexive:
            dot.edge(cls_id, cls_id, label=label, style=style, arrowhead=arrowhead)
        else:
            # Association to a superclass shares endpoints with the generalization
            # edge; route via east ports so the arrows do not overlap.
            edge_attrs = {"label": label, "style": style, "arrowhead": arrowhead}
            if dest_id in super_id_set or source_id in super_id_set:
                edge_attrs["constraint"] = "false"
                dot.edge(f"{source_id}:e", f"{dest_id}:e", **edge_attrs)
            else:
                dot.edge(source_id, dest_id, **edge_attrs)

    # Figure note for multilingual (uniqueLang / *MultilingualShape) attributes —
    # placed after associations so it sits at the bottom of the figure.
    if needs_unique_lang_note:
        note_id = f"{cls_id}_uniqueLang_note"
        dot.node(
            note_id,
            label=(
                '<<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="0" CELLPADDING="2">'
                '<TR><TD ALIGN="LEFT"><I>¹ Limited to one rdf:langString value per language</I></TD></TR>'
                '</TABLE>>'
            ),
            margin="0",
        )
        anchor = assoc_nodes[-1] if assoc_nodes else cls_id
        dot.edge(anchor, note_id, style="invis")

    # Save and render
    try:
        dot_file = os.path.join(diagrams_dir, f"{cls_filename}.dot")
        dot.save(dot_file)
        dot.render(dot_file, cleanup=False)
        dot.render(dot_file, format='png', cleanup=False)
        log.debug(f"Generated diagram for {cls_name}")
    except Exception as e:
        error_msg = f"Error rendering diagram for {cls_name} from {ofn_path}: {str(e)}\n{traceback.format_exc()}"
        errors.append(error_msg)
        log.error(error_msg)
        raise