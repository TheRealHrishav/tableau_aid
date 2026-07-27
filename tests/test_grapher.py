import pandas as pd
import pytest

from graph_smith.grapher import build_dag, field_report, generate_tiered_sql, transpile


def _df(rows):
    return pd.DataFrame(rows, columns=["internal_name", "field_name", "formula"])


def test_build_dag_creates_edge_for_dependency():
    df = _df([
        ["[Calc_1]", "Base", None],
        ["[Calc_2]", "Derived", "[Calc_1] + 1"],
    ])
    g, mapping, pattern = build_dag(df)

    assert set(g.nodes) == {"[Calc_1]", "[Calc_2]"}
    assert g.has_edge("[Calc_1]", "[Calc_2]")
    assert mapping["[Calc_1]"] == "Base"


def test_build_dag_matches_longest_name_first():
    # Without length-sorted matching, "[Calc_1]" could wrongly match inside "[Calc_10]".
    df = _df([
        ["[Calc_1]", "Base", None],
        ["[Calc_10]", "Other", None],
        ["[Calc_2]", "Derived", "[Calc_10] + 1"],
    ])
    g, _, _ = build_dag(df)

    assert g.has_edge("[Calc_10]", "[Calc_2]")
    assert not g.has_edge("[Calc_1]", "[Calc_2]")


def test_build_dag_raises_on_duplicate_internal_names():
    df = _df([
        ["[Calc_1]", "Base", None],
        ["[Calc_1]", "Base Dup", None],
    ])
    with pytest.raises(ValueError):
        build_dag(df)


def test_generate_tiered_sql_raises_on_cycle():
    df = _df([
        ["[Calc_1]", "A", "[Calc_2]"],
        ["[Calc_2]", "B", "[Calc_1]"],
    ])
    df["presto_logic"] = ['"B"', '"A"']
    g, _, _ = build_dag(df)

    with pytest.raises(ValueError):
        generate_tiered_sql(df, g)


def test_generate_tiered_sql_orders_layers_by_dependency():
    df = _df([
        ["[Calc_1]", "Base", None],
        ["[Calc_2]", "Derived", "[Calc_1] + 1"],
    ])
    df["presto_logic"] = ['"Base"', '"Base" + 1']
    g, _, _ = build_dag(df)

    sql = generate_tiered_sql(df, g)

    assert sql.startswith("WITH layer_0")
    assert '"Base" AS "Base"' in sql
    assert sql.index('"Base" AS "Base"') < sql.index('"Base" + 1 AS "Derived"')
    assert sql.rstrip().endswith("SELECT * FROM layer_1")


def test_field_report_translates_formulas_and_assigns_layers():
    df = _df([
        ["[Calc_1]", "Base", None],
        ["[Calc_2]", "Derived", "[Calc_1] + 1"],
    ])
    report = field_report(df)

    assert list(report["cte_layer"]) == [1, 2]
    base_row = report[report["field_name"] == "Base"].iloc[0]
    derived_row = report[report["field_name"] == "Derived"].iloc[0]
    assert base_row["sql_logic"] == "NULL"
    assert derived_row["sql_logic"] == '("Base" + 1)'


def test_field_report_does_not_mutate_input_dataframe():
    df = _df([["[Calc_1]", "Base", None]])
    original = df.copy()
    field_report(df)
    pd.testing.assert_frame_equal(df, original)


def test_transpile_returns_sql_and_no_errors_for_valid_formulas():
    df = _df([
        ["[Calc_1]", "Base", None],
        ["[Calc_2]", "Derived", "[Calc_1] + 1"],
    ])
    sql, errors = transpile(df)

    assert errors == []
    assert "WITH" in sql
    assert "SELECT * FROM" in sql


def test_transpile_collects_errors_for_unparseable_formulas():
    df = _df([["[Calc_1]", "Broken", "[Sales] +"]])

    sql, errors = transpile(df)

    assert len(errors) == 1
    assert errors[0]["field_name"] == "Broken"
    assert "error" in errors[0]
    assert '"Broken"' in sql  # column still generated, rendered as NULL


def test_transpile_does_not_mutate_input_dataframe():
    df = _df([["[Calc_1]", "Base", None]])
    original = df.copy()
    transpile(df)
    pd.testing.assert_frame_equal(df, original)
