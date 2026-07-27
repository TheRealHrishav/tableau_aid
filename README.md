# tableau-aid

Prototype tool that converts Tableau calculated fields into a single, dependency-ordered
Presto SQL query.

Tableau lets calculated fields reference each other in any order. To run the same logic
in a SQL warehouse, those dependencies need to be resolved into a valid evaluation order,
and each formula needs to be translated from Tableau's calculation language into SQL.
graph-smith does both:

1. **Extract** calculated fields (and their formulas) out of a `.twbx` workbook.
2. **Graph** the fields into a dependency DAG and topologically sort them into layers.
3. **Transpile** each Tableau formula into a Presto SQL expression.
4. **Generate** a single `WITH ... SELECT` query with one CTE per dependency layer.

## Status

Prototype. The formula transpiler (`transpiler.py`) covers common Tableau syntax
(`IF`/`CASE`, arithmetic, comparisons, `IN`, boolean logic, a handful of functions like
`ZN`/`ISNULL`/type casts). LOD expressions and parameters are recognized but not translated
— they render as placeholders. `replace.py` (in-place `.twb` field replacement/remapping)
is still under active development.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
```

## Usage

```python
from graph_smith.extractor import extract_and_parse
from graph_smith.grapher import transpile
import pandas as pd

calculated_fields, non_calculated_fields = extract_and_parse("workbook.twbx")

df = pd.DataFrame(calculated_fields)
# df needs columns: internal_name, field_name, formula
# (grapher.transpile computes "sql_logic" internally; generate_tiered_sql
#  expects a "presto_logic" column — see field_report() below if you need
#  the intermediate per-field translations first)

sql, errors = transpile(df, parameter_names=frozenset({"Min Date"}))
print(sql)

for e in errors:
    print(f"Failed to translate {e['field_name']}: {e['error']}")
```

To inspect per-field translations and dependency layers before generating the final
query, use `field_report`:

```python
from graph_smith.grapher import field_report

report = field_report(df)
# columns: field_name, internal_name, formula, sql_logic, cte_layer
```

### Module overview

| Module | Purpose |
|---|---|
| `extractor.py` | Unzips a `.twbx` and parses the inner `.twb` XML into calculated / non-calculated field records. |
| `grapher.py` | Builds the dependency DAG from field formulas, and orchestrates translation + tiered SQL generation. |
| `transpiler.py` | Grammar (via `lark`) and transformer that translate a single Tableau formula string into a Presto SQL expression. |
| `replace.py` | Rewrites a `.twb`'s calculation metadata to replace/remap calculated fields in place, repackaging into a new `.twbx`. (WIP) |

## Running tests

```bash
pytest
```

## Known limitations

- LOD expressions (`{FIXED ... : ...}`) and parameters are not translated to real SQL —
  they render as descriptive placeholder text.
- `replace.py` matches Tableau's XML with regular expressions rather than an XML parser,
  so it's sensitive to formatting variations in the `.twb` file. Treat it as experimental.
- The transpiler's function support is limited to what's explicitly mapped in
  `TableauToPrestoNodes.function` (`ZN`, `ISNULL`, and a handful of type casts); anything
  else passes through as a same-named SQL function call, which may not exist in Presto.
