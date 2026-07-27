import pytest

from graph_smith.transpiler import translate


def test_field_reference_replaces_spaces_with_underscores():
    assert translate("[Order Date]") == '"Order_Date"'


def test_number_literal():
    assert translate("42") == "42"


def test_string_literal_double_quotes_converted_to_single():
    assert translate('"hello"') == "'hello'"


def test_arithmetic_expression():
    assert translate("[Sales] + [Tax]") == '("Sales" + "Tax")'


def test_comparison():
    assert translate("[Sales] > 100") == '"Sales" > 100'


def test_boolean_and_or():
    result = translate("[A] > 1 AND [B] < 2")
    assert result == '("A" > 1 AND "B" < 2)'


def test_not():
    assert translate("NOT [Flag]") == 'NOT "Flag"'


def test_in_clause():
    assert translate("[Region] IN ('East', 'West')") == '"Region" IN (\'East\', \'West\')'


def test_if_then_else_end():
    result = translate("IF [Sales] > 0 THEN 'Positive' ELSE 'Non-positive' END")
    assert result == "CASE WHEN \"Sales\" > 0 THEN 'Positive' ELSE 'Non-positive' END"


def test_case_expression_expands_to_searched_case():
    result = translate("CASE [Region] WHEN 'East' THEN 1 ELSE 0 END")
    assert result == "CASE WHEN \"Region\" = 'East' THEN 1 ELSE 0 END"


def test_zn_function_becomes_coalesce():
    assert translate("ZN([Sales])") == 'COALESCE("Sales", 0)'


def test_isnull_function():
    assert translate("ISNULL([Sales])") == '"Sales" IS NULL'


def test_isnull_equals_true_simplifies_to_is_null():
    assert translate("ISNULL([Sales]) = TRUE") == '"Sales" IS NULL'


def test_isnull_equals_false_simplifies_to_is_not_null():
    assert translate("ISNULL([Sales]) = FALSE") == '"Sales" IS NOT NULL'


def test_type_cast_functions():
    assert translate("INT([Sales])") == 'CAST("Sales" AS INTEGER)'
    assert translate("STR([Sales])") == 'CAST("Sales" AS VARCHAR)'


def test_unmapped_function_passes_through_uppercased():
    assert translate("sum([Sales])") == 'SUM("Sales")'


def test_lod_expression_renders_placeholder():
    result = translate("{ FIXED [Region] : SUM([Sales]) }")
    assert result == "(the calculated field contained an LOD expression)"


def test_parameter_renders_placeholder():
    result = translate("[Min Date]", parameter_names=frozenset({"Min Date"}))
    assert result == "(the calculated field contained a parameter)"


def test_invalid_expression_raises_value_error():
    with pytest.raises(ValueError):
        translate("[Sales] +")


def test_non_breaking_space_is_normalized():
    # Tableau formulas sometimes contain non-breaking spaces (\xa0) between tokens.
    result = translate("[Sales]\xa0+\xa0[Tax]")
    assert result == '("Sales" + "Tax")'
