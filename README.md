# ont2md

This project provides scripts to generate a [MkDocs](https://www.mkdocs.org/) site (with ODM-style diagrams) from RDF sources in `docs/`.

| Entry point | Source format |
| ----------- | ------------- |
| `python/ttl2md.py` | Turtle (`.ttl`) |
| `python/owl2md.py` | RDF/XML (`.owl`) |
| `python/ofn2md.py` | OWL Functional Syntax (`.ofn`; requires `funowl`) |

All three share the same pipeline: unified graph load, `owl:imports` resolution (catalog / optional `--dev` map / HTTP), registries, diagrams, Markdown, MkDocs nav, and optional ReqView CSV.

## Prerequisites

Run the script from the **project root** (the directory that contains `mkdocs.yml` and `docs/`).

| Requirement | Purpose |
| ----------- | ------- |
| `mkdocs.yml` | Site configuration; navigation is rewritten on each run. A sample mkdocs.yml file can be found in any of the ISO-TC204/ontology repositories |
| `docs/` | Source `.ttl` files and generated Markdown output |
| Python 3 | Runtime |
| [RDFLib](https://rdflib.readthedocs.io/) | Parse and query Turtle |
| [Graphviz](https://graphviz.org/) (`dot` on `PATH`) | Render class diagrams |
| PyYAML | Update `mkdocs.yml` navigation |

Install Python dependencies (minimum):

```bash
pip install rdflib pyyaml graphviz
```

For building the site locally, also install MkDocs (for example [Material for MkDocs](https://squidfunk.github.io/mkdocs-material/)) and any plugins referenced in your `mkdocs.yml`.

## Usage

```bash
cd /path/to/your-mkdocs-project   # must contain mkdocs.yml and docs/
python python/ttl2md.py
```

Optional flags:

| Flag | Meaning |
| ---- | ------- |
| `--create-missing` or `-c` | ReqView CSV includes concepts **without** `its-core:reqviewId` (empty `id` column for new ReqView objects). Default: only concepts that already have a ReqView ID. |
| `--dev` or `-dev` | Remap `owl:imports` through a **local per-user map file** instead of fetching mapped IRIs from the network. |
| `--dev-map PATH` | Path to that map (implies `--dev`). Default search: `./dev-iri-map.yml` (also `.yaml` / `.json` / `.ont2md-dev-map.*`). |

The use of ReqView for tracing ontology concepts to use cases is entirely optional but has been provided for use within ISO TC 204.

No other command-line arguments are accepted. Extra arguments print usage and exit with code `1`.

### Exit behaviour

- **Missing `mkdocs.yml` or `docs/`** — error message, exit `1`
- **`--dev` without a map file** — error message, exit `1`
- **Invalid Turtle syntax** in a pattern file or optional shared SHACL file — error with line context, exit `2` (no partial site generation)
- **No `.ttl` files in `docs/`** — message and exit `0`
- **Per-class or nav/CSV errors** — logged; other outputs may still be written

## Turtle files in `docs/`

All files matching `docs/*.ttl` (case-insensitive extension) are loaded into one **unified RDF graph**. How each file is treated depends on its **basename**.

### File naming rules

| Pattern | Example | Role |
| ------- | ------- | ---- |
| `*-pattern.ttl` | `fuzzy-time-pattern.ttl` | **Pattern module** — overview page, nav section, and classes/properties declared in that file |
| `*-shacl.ttl` | `fuzzy-time-shacl.ttl` | **SHACL constraints** — merged into diagrams and formalization; not a separate nav “pattern” |
| `*-reqview.ttl` | `its-time-reqview.ttl` | **ReqView sidecar** — `its-core:reqviewId` annotations; excluded from site index, pattern pages, and MkDocs nav |
| Other `.ttl` | `core.ttl`, `its-time.ttl` | **Ontology modules** — metadata and locally declared classes/properties; may serve as the site “home” module |

Pattern module ontology names are in UpperCamelCase (for example `fuzzy-time-pattern.ttl` → name of ontology connect within file: `:FuzzyTimePattern`).

### Namespace and “local” concepts

The script determines the **master namespace** from, in order:

1. A `BASE <...>` declaration in any loaded file
2. `vann:preferredNamespaceUri` on an `owl:Ontology`
3. The empty prefix (`@prefix : <...>` / `PREFIX : <...>`)
4. Fallback: `https://example.org/`

Only classes and properties whose IRIs start with that namespace are documented as local pages. Imported concepts appear in diagrams and links but do not get their own generated pages unless they are in the unified graph under that namespace.

Each pattern/module file should use a consistent `BASE` and preferred namespace metadata, as in the sample files under `docs/`.

### Recommended ontology metadata

On each `owl:Ontology` (especially in pattern and home modules):

| Property | Used for |
| -------- | -------- |
| `skos:title`, `dcterms:title`, or `schema:name` | Pattern/module title and nav labels |
| `skos:definition`, `schema:description`, or `dcterms:description` | Overview and index text |
| `vann:preferredNamespaceUri` | Namespace resolution |
| `vann:preferredNamespacePrefix` | Home module selection, ReqView CSV filename |
| `its-core:draft` (`true`/`false`) | Draft banner on generated pages |

On classes, prefer `skos:definition` (or `schema:description`), `skos:example`, and `skos:note` where applicable.

### ReqView traceability

- Annotate concepts with `its-core:reqviewId` (typically in a `*-reqview.ttl` file).
- After each run, the script writes `docs/traceability/<preferredNamespacePrefix>.csv` for manual import into ReqView (“Update existing objects”).
- Without `--create-missing`, rows are emitted only for concepts that already have a ReqView ID.
- `ITSThing` and `TimeThing` are omitted from the CSV.

### owl:imports

After loading every `docs/*.ttl` file, the script recursively follows `owl:imports`:

1. Skip IRIs already present as an `owl:Ontology` from local files
2. If `--dev` / `--dev-map` was given, remap via the local per-user IRI map
3. Resolve via an OASIS XML catalog if present (`catalog-v001.xml` / `catalog.xml` in the project root or `docs/`, or `$ONT2MD_CATALOG`)
4. Otherwise fetch the import IRI over HTTP(S) (with retries) or open a local path

Imported graphs contribute constraints and external types used in diagrams. **Generated class pages stay limited to the project’s master namespace**, so documentation does not spawn pages for every foreign class.

Remote fetches (especially via `w3id.org` redirects) can fail transiently with TLS errors such as `SSL: UNEXPECTED_EOF_WHILE_READING`. The script retries several times; for offline work use `--dev` with a local map.

Catalog entries should use portable relative paths (resolved against the catalog file’s directory), not machine-specific absolute paths.

#### Dev mode (`--dev`) — local IRI map

Normal runs (no `--dev`) always use published IRIs / catalogs. Local remaps are **opt-in only**, so you cannot accidentally stay on checkouts when intending to exercise the live site.

1. Copy `dev-iri-map.example.yml` → `dev-iri-map.yml` next to the tool, or to `~/.config/ont2md/dev-iri-map.yml` (recommended so every ontology project can share one map). The personal file is gitignored.
2. Point each IRI at a local checkout directory or ontology file.
3. Run from the ontology project (or the tool checkout):

```bash
python /path/to/ont2md/python/ttl2md.py --dev
# or: python python/ttl2md.py --dev-map ~/.config/ont2md/dev-iri-map.yml
```

Search order for `--dev`: current project → tool install directory → `~/.config/ont2md/` → `dev-iri-map.example.yml` as a last resort (with a note). The script prints a clear `*** DEV MODE ***` banner when remaps are active. Omit `--dev` to fetch live published ontologies again.

Example map entries:

```yaml
https://w3id.org/itsdata/vehicle/v1/: ~/GitHub/ontology-its-vehicle
https://w3id.org/citydata/part1/v1/: ~/GitHub/ontology-cdm-p1/docs/cdm1.ttl
```

### Concept and ontology registries

On each successful TTL load, `ttl2md.py` updates markdown registries next to the script:

| File | Contents |
| ---- | -------- |
| `python/concept_registry.md` | Classes and properties seen in the unified graph (local + imported) |
| `python/ontology_registry.md` | `owl:Ontology` IRIs sorted by Official IRI; Prefix links to `https://isotc204.org/<repo>` (e.g. `ontology-its-core-v1`, `ontology-cdm-p1`) |

New entries are appended; existing IRI keys are left unchanged except that entries under the
**master namespace of the ontology being processed** are pruned when they no longer appear in the
loaded graph (so renamed/removed local concepts do not keep regenerating pages). Both filenames
are listed in `.gitignore` (local tooling artifacts). The OWL/OFN processors already wrote these
files; the TTL path now does as well.

### Concept registry (manual extras)

## What the script generates

From the project root, under `docs/`:

| Output | Description |
| ------ | ----------- |
| `index.md` | Site home (module chosen by `vann:preferredNamespacePrefix` and file layout; see `resolve_home_ontology` in `utils.py`) |
| `classes/<ClassName>.md` | One page per **local** class (name = URI local name, e.g. `FuzzyTime`) |
| `classes/<ModuleName>.md` | Pattern overview for each `*-pattern.ttl` module |
| `properties/<prefix:local>.md` | One page per local object/datatype property |
| `diagrams/<ClassName>.dot.svg` (and related `.dot` / `.png`) | ODM-style diagrams (OWL + SHACL merged) |
| `traceability/<prefix>.csv` | ReqView update export |

Before writing new pages, `docs/classes/`, `docs/properties/`, and `docs/diagrams/` are cleared and recreated so renamed or removed concepts do not leave stale Markdown or diagram files behind. Clearing happens only after Turtle has been parsed successfully.

`mkdocs.yml` **`nav`** is replaced to reflect patterns (or a flat Classes/Properties layout when no `*-pattern.ttl` files exist).

### Optional top-level navigation (`top-nav.yml`)

If the project root (next to `mkdocs.yml`) contains **`top-nav.yml`** (or `top-nav.yaml`), those entries are prepended as top-level nav links, and the generated Home / Classes / Properties / pattern sections are nested under a section named after `site_name` (falling back to the project directory name).

Example (`top-nav.example.yml` in this repo):

```yaml
- TC204 on ISO.org: https://www.iso.org/committee/54706.html
- TC 204 Home: https://isotc204.org/
```

That yields navigation like:

```yaml
nav:
- TC204 on ISO.org: https://www.iso.org/committee/54706.html
- TC 204 Home: https://isotc204.org/
- ITS Ontology - Core:    # from site_name
  - Home: index.md
  - Classes: ...
  - Properties: ...
```

If the file is absent, navigation is unchanged from the previous flat layout.

## Typical workflow

1. Edit Turtle under `docs/` (patterns, SHACL, ReqView sidecars as needed).
2. From the project root, run `python python/ttl2md.py` (add `-c` only when intentionally creating new ReqView objects).
3. Review generated Markdown and diagrams under `docs/`.
4. Build or deploy the site with MkDocs (`mkdocs serve` / `mkdocs build` or your CI workflow).

## Sample layout

This repository’s `docs/` folder illustrates the conventions:

- `<preferred-prefix>.ttl` - (e.g. `its-time.ttl`) imports all pattern ttl files into master for namespace and home metadata (`vann:preferredNamespacePrefix` `its-time`)
- `core-pattern.ttl` — core module defining concepts that need to be imported by all others (e.g., TimeThing)
- `fuzzy-time-pattern.ttl` / `schedule-pattern.ttl` — pattern OWL
- `fuzzy-time-shacl.ttl` / `schedule-shacl.ttl` — SHACL shapes
- `its-time-reqview.ttl` — ReqView IDs
