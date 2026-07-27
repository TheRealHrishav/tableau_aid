import re
from typing import Any

import networkx as nx
import pandas as pd

from graph_smith.transpiler import translate


# ==========================================
# 1. DAG CONSTRUCTION
# ==========================================


def _build_name_mapping(
    internal_names: pd.Series,
    field_names: pd.Series,
) -> tuple[dict[str, str], re.Pattern]:
    """Build internal-name → field-name mapping and a length-sorted regex pattern.

    Sorting by length descending ensures '[Calc_10]' matches before '[Calc_1]'.
    """
    mapping = dict(zip(internal_names, field_names))
    sorted_keys = sorted(mapping, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(k) for k in sorted_keys))
    return mapping, pattern


def build_dag(df: pd.DataFrame) -> tuple[nx.DiGraph, dict[str, str], re.Pattern]:
    """Build a dependency DAG from a DataFrame of Tableau calculated fields.

    Nodes are internal field names. Edges run dependency → dependent so that
    topological order gives fields that are safe to compute at each step.

    Args:
        df: Must contain columns 'internal_name', 'field_name', 'formula'.

    Raises:
        ValueError: if any internal_name values are duplicated.
    """
    dupes = df.loc[df["internal_name"].duplicated(), "internal_name"].tolist()
    if dupes:
        raise ValueError(f"Duplicate internal names: {dupes}")

    mapping, pattern = _build_name_mapping(df["internal_name"], df["field_name"])

    g = nx.DiGraph()
    for _, row in df.iterrows():
        node_id = row["internal_name"]
        formula = "" if pd.isna(row["formula"]) else str(row["formula"])

        g.add_node(node_id, field_name=row["field_name"], formula=formula)

        for dep in set(pattern.findall(formula)):
            if dep != node_id:
                g.add_edge(dep, node_id)

    return g, mapping, pattern


# ==========================================
# 2. FORMULA TRANSLATION
# ==========================================


def _sanitize_name(name: str) -> str:
    """Apply the same normalisation the transpiler uses on field tokens."""
    if not name:
        return ""
    return str(name).replace(" ", "_")


def _translate_formula(
    raw_formula: Any,
    mapping: dict[str, str],
    pattern: re.Pattern,
    parameter_names: frozenset[str] = frozenset(),
) -> str:
    """Substitute internal names and transpile one Tableau formula to Presto SQL.

    Substitution preserves bracket syntax so the Tableau grammar still
    recognises references as field tokens before translation.

    NaN / None formulas are rendered as NULL.
    """
    if pd.isna(raw_formula):
        return "NULL"

    def _substitute(match: re.Match) -> str:
        field_name = _sanitize_name(mapping[match.group(0)])
        return f"[{field_name}]"

    substituted = pattern.sub(_substitute, str(raw_formula))
    substituted = re.sub(r"//[^\n]*", "", substituted)
    substituted = re.sub(r"\[Parameters\]\.", "", substituted)
    substituted = re.sub(r"(?<![\]\)\w])-(?=\[)", "0-", substituted)

    return translate(substituted, parameter_names=parameter_names)


# ==========================================
# 3. SQL GENERATION
# ==========================================


def generate_tiered_sql(df: pd.DataFrame, g: nx.DiGraph) -> str:
    """Generate a layered CTE query from the dependency DAG.

    Each topological generation becomes one CTE that carries forward all
    previous columns plus the newly computed fields for that layer.

    Raises:
        ValueError: if the graph contains a cycle.
    """
    try:
        layers = list(nx.topological_generations(g))
    except nx.NetworkXUnfeasible as exc:
        raise ValueError("Circular dependency detected in calculated fields") from exc

    logic_map = dict(zip(df["internal_name"], df["presto_logic"]))
    name_map = {
        nid: _sanitize_name(name)
        for nid, name in zip(df["internal_name"], df["field_name"])
    }

    cte_parts: list[str] = []
    prev_table = "source_data"

    for i, layer_nodes in enumerate(layers):
        col_exprs = ["*"] + [
            f'{logic_map[nid]} AS "{name_map[nid]}"'
            for nid in layer_nodes
        ]
        col_block = ",\n        ".join(col_exprs)
        step_name = f"layer_{i}"
        cte_parts.append(
            f"{step_name} AS (\n"
            f"    SELECT\n"
            f"        {col_block}\n"
            f"    FROM {prev_table}\n"
            f")"
        )
        prev_table = step_name

    return "WITH " + ",\n".join(cte_parts) + f"\nSELECT * FROM {prev_table}"


def field_report(
    df: pd.DataFrame,
    parameter_names: frozenset[str] = frozenset(),
) -> pd.DataFrame:
    """Return a DataFrame mapping each field to its translated SQL and CTE layer.

    Fully self-contained: runs translation internally so callers do not need
    to call transpile first.

    Args:
        df: DataFrame with columns: internal_name, field_name, formula.
        parameter_names: Raw names of Tableau parameters (same as transpile).

    Returns:
        DataFrame with columns: field_name, internal_name, formula,
        sql_logic, cte_layer (1-indexed).

    Raises:
        ValueError: if the graph contains a cycle.
    """
    df = df.copy()

    g, mapping, pattern = build_dag(df)

    try:
        layers = list(nx.topological_generations(g))
    except nx.NetworkXUnfeasible as exc:
        raise ValueError("Circular dependency detected in calculated fields") from exc

    layer_map = {
        node: i + 1
        for i, layer_nodes in enumerate(layers)
        for node in layer_nodes
    }

    def _safe_translate(row):
        try:
            return _translate_formula(row["formula"], mapping, pattern, parameter_names)
        except Exception:
            return "NULL"

    df["sql_logic"] = df.apply(_safe_translate, axis=1)
    df["cte_layer"] = df["internal_name"].map(layer_map)

    return df[["field_name", "internal_name", "formula", "sql_logic", "cte_layer"]].reset_index(drop=True)


# ==========================================
# 4. ORCHESTRATOR
# ==========================================


def transpile(
    df: pd.DataFrame,
    parameter_names: frozenset[str] = frozenset(),
) -> tuple[str, list[dict]]:
    """Transpile Tableau calculated fields to a layered Presto SQL CTE query.

    Args:
        df: DataFrame with columns: internal_name, field_name, formula.
        parameter_names: Raw names of Tableau parameters as they appear between
            brackets in formulas (e.g. frozenset({"Min Date", "Max Revenue"})).
            These render as a placeholder rather than a column reference.

    Returns:
        A tuple of (sql, errors) where sql is a valid Presto SQL string with one
        CTE per dependency layer, and errors is a list of dicts describing fields
        that failed to translate (keys: internal_name, field_name, formula,
        error). Failed fields are rendered as NULL.
    """
    df = df.copy()  # never mutate the caller's DataFrame

    g, mapping, pattern = build_dag(df)

    errors: list[dict] = []

    def _safe_translate(row):
        try:
            return _translate_formula(row["formula"], mapping, pattern, parameter_names)
        except Exception as exc:
            errors.append({
                "internal_name": row["internal_name"],
                "field_name": row["field_name"],
                "formula": row["formula"],
                "error": str(exc),
            })
            return "NULL"


    df["presto_logic"] = df.apply(_safe_translate, axis=1)

    return generate_tiered_sql(df, g), errors
