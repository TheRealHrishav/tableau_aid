from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from lark import Lark, Token, Transformer, Tree


# ==========================================
# 1. NODE DEFINITIONS
# ==========================================


class SqlNode(ABC):
    @abstractmethod
    def render(self) -> str: ...


@dataclass
class FieldNode(SqlNode):
    name: str

    def render(self) -> str:
        return f'"{self.name}"'


@dataclass
class LiteralNode(SqlNode):
    value: Any

    def render(self) -> str:
        return str(self.value)


@dataclass
class ComparisonNode(SqlNode):
    left: SqlNode
    op: str
    right: SqlNode

    def render(self) -> str:
        return f"{self.left.render()} {self.op} {self.right.render()}"


@dataclass
class InNode(SqlNode):
    left: Optional[SqlNode]
    values: list[SqlNode]

    def render(self) -> str:
        if not self.values:
            raise ValueError("IN clause requires at least one value")
        val_list = ", ".join(v.render() for v in self.values)
        left_side = self.left.render() if self.left else ""
        return f"{left_side} IN ({val_list})"


@dataclass
class NotNode(SqlNode):
    operand: SqlNode

    def render(self) -> str:
        return f"NOT {self.operand.render()}"


@dataclass
class LogicalNode(SqlNode):
    operator: str
    items: list[SqlNode]

    def render(self) -> str:
        parts = [i.render() for i in self.items if isinstance(i, SqlNode)]
        if len(parts) == 1:
            return parts[0]
        return f"({f' {self.operator} '.join(parts)})"


@dataclass
class CaseNode(SqlNode):
    conditions: list[tuple[SqlNode, SqlNode]] = field(default_factory=list)
    else_result: Optional[SqlNode] = None

    def render(self) -> str:
        if not self.conditions:
            raise ValueError("CASE expression requires at least one WHEN clause")
        clauses = " ".join(
            f"WHEN {c.render()} THEN {r.render()}" for c, r in self.conditions
        )
        sql = f"CASE {clauses}"
        if self.else_result:
            sql += f" ELSE {self.else_result.render()}"
        return sql + " END"

class ParameterNode(SqlNode):
    def __init__(self, name: str) -> None:
        self.name = name

    def render(self) -> str:
        return "(the calculated field contained a parameter)"


class LodNode(SqlNode):
    def render(self) -> str:
        return "(the calculated field contained an LOD expression)"


@dataclass
class IsNullNode(SqlNode):
    operand: SqlNode
    negated: bool = False

    def render(self) -> str:
        suffix = "IS NOT NULL" if self.negated else "IS NULL"
        return f"{self.operand.render()} {suffix}"



# ==========================================
# 2. GRAMMAR
# ==========================================

TABLEAU_GRAMMAR = r"""
    ?start: expr
    ?expr: if_expr | case_expr | logic_expr
    if_expr: IF expr THEN expr (ELSEIF expr THEN expr)* (ELSE expr)? END
    case_expr: CASE expr (WHEN expr THEN expr)+ (ELSE expr)? END

    ?logic_expr: logic_term (OR logic_term)*
    ?logic_term: logic_factor (AND logic_factor)*
    ?logic_factor: NOT? comparison

    ?comparison: arith_expr (OP arith_expr | in_clause)?
    in_clause: IN "(" expr ("," expr)* ")"

    ?arith_expr: term ((ADD | SUB) term)*
    ?term: factor ((MUL | DIV) factor)*
    ?factor: NUMBER | STRING | BOOLEAN | NULL | field | function | lod_expr | "(" expr ")"

    field: "[" /[^\]]+/ "]"
    function: IDENT "(" [expr ("," expr)*] ")"

    // LOD: { FIXED|INCLUDE|EXCLUDE [dim], [dim], ... : expr }
    // Dimensions are optional (e.g. { FIXED : SUM([Sales]) } is valid)
    lod_expr: "{" LOD_KEYWORD (field ("," field)*)? ":" expr "}"

    CASE.2:    /CASE/i
    WHEN.2:    /WHEN/i
    IF.2:      /IF/i
    THEN.2:    /THEN/i
    ELSEIF.2:  /ELSEIF/i
    ELSE.2:    /ELSE/i
    END.2:     /END/i
    AND.2:     /AND/i
    OR.2:      /OR/i
    NOT.2:     /NOT/i
    BOOLEAN.2: /TRUE|FALSE/i
    NULL.2:    /NULL/i
    IN.2:      /IN/i
    LOD_KEYWORD.2: /FIXED|INCLUDE|EXCLUDE/i

    ADD: "+"
    SUB: "-"
    MUL: "*"
    DIV: "/"

    OP:    "=" | "<>" | "!=" | "<=" | ">=" | "<" | ">"
    IDENT: /[a-zA-Z_][a-zA-Z0-9_]*/

    %import common.NUMBER
    %import common.WS
    %ignore WS
    STRING: /'[^']*'/ | /"[^"]*"/
"""


# ==========================================
# 3. TRANSFORMER
# ==========================================


class TableauToPrestoNodes(Transformer):
    """Transforms a Lark parse tree into renderable Presto SQL nodes."""

    def __init__(self, parameter_names: frozenset[str] = frozenset()) -> None:
        super().__init__()
        self._parameter_names = parameter_names

    def field(self, items: list) -> SqlNode:
        raw_name = str(items[0])          # brackets already stripped by Lark
        if raw_name in self._parameter_names:
            return ParameterNode(raw_name)
        return FieldNode(raw_name.replace(" ", "_"))

    def lod_expr(self, items: list) -> LodNode:
        return LodNode()                  # content is untranslatable; discard it

    def _to_node(self, item: Any) -> SqlNode:
        if isinstance(item, SqlNode):
            return item
        if isinstance(item, Tree):
            transformed = self.transform(item)
            if isinstance(transformed, list):
                transformed = transformed[0]
            return self._to_node(transformed)
        return LiteralNode(str(item))

    def _binary_expr(self, items: list) -> SqlNode:
        if len(items) == 1:
            return self._to_node(items[0])
        expr = " ".join(
            i.render() if isinstance(i, SqlNode) else str(i) for i in items
        )
        return LiteralNode(f"({expr})")

    # --- Terminals ---

    def ADD(self, t: Token) -> str: return str(t)
    def SUB(self, t: Token) -> str: return str(t)
    def MUL(self, t: Token) -> str: return str(t)
    def DIV(self, t: Token) -> str: return str(t)

    def NUMBER(self, t: Token) -> LiteralNode: return LiteralNode(str(t))
    def STRING(self, t: Token) -> LiteralNode:
        s = str(t)
        if s.startswith('"') and s.endswith('"'):
            inner = s[1:-1].replace("'", "''")
            s = f"'{inner}'"
        return LiteralNode(s)
    def BOOLEAN(self, t: Token) -> LiteralNode: return LiteralNode(str(t).upper())
    def NULL(self, t: Token) -> LiteralNode: return LiteralNode("NULL")

    # --- Rules ---

    def arith_expr(self, items: list) -> SqlNode:
        return self._binary_expr(items)

    def term(self, items: list) -> SqlNode:
        return self._binary_expr(items)

    def function(self, items: list) -> SqlNode:
        name = str(items[0]).upper()
        args = [self._to_node(a) for a in items[1:]]
        if name == "ZN":
            return LiteralNode(f"COALESCE({args[0].render()}, 0)")
        if name == "ISNULL":
            return IsNullNode(operand=args[0])
        cast_types = {
            "FLOAT": "DOUBLE",
            "INT": "INTEGER",
            "STR": "VARCHAR",
            "DATE": "DATE",
            "DATETIME": "TIMESTAMP",
            "BOOL": "BOOLEAN",
        }
        if name in cast_types:
            return LiteralNode(f"CAST({args[0].render()} AS {cast_types[name]})")
        arg_str = ", ".join(a.render() for a in args)
        return LiteralNode(f"{name}({arg_str})")

    def comparison(self, items: list) -> SqlNode:
        if len(items) == 1:
            return self._to_node(items[0])
        left = self._to_node(items[0])
        second = items[1]
        if isinstance(second, InNode):
            second.left = left
            return second
        right = self._to_node(items[2])
        op = str(second)
        if isinstance(left, IsNullNode) and op in ("=", "<>", "!="):
            rendered = right.render().upper()
            if rendered in ("TRUE", "FALSE"):
                truthy = rendered == "TRUE"
                if op != "=":
                    truthy = not truthy
                return IsNullNode(operand=left.operand, negated=not truthy)
        return ComparisonNode(left=left, op=op, right=right)

    def in_clause(self, items: list) -> InNode:
        # items[0] is the IN Token; remaining items are the expression nodes
        values = [self._to_node(i) for i in items[1:]]
        return InNode(left=None, values=values)

    def logic_expr(self, items: list) -> SqlNode:
        nodes = [self._to_node(i) for i in items if not isinstance(i, Token)]
        return nodes[0] if len(nodes) == 1 else LogicalNode("OR", nodes)

    def logic_term(self, items: list) -> SqlNode:
        nodes = [self._to_node(i) for i in items if not isinstance(i, Token)]
        return nodes[0] if len(nodes) == 1 else LogicalNode("AND", nodes)

    def logic_factor(self, items: list) -> SqlNode:
        if len(items) == 2:  # NOT <expr>
            return NotNode(self._to_node(items[1]))
        return self._to_node(items[0])

    def if_expr(self, items: list) -> CaseNode:
        node = CaseNode()
        it = iter(items)
        for token in it:
            keyword = str(token).upper()
            if keyword in ("IF", "ELSEIF"):
                cond = self._to_node(next(it))
                next(it)  # THEN token
                result = self._to_node(next(it))
                node.conditions.append((cond, result))
            elif keyword == "ELSE":
                node.else_result = self._to_node(next(it))
            # END token: falls through harmlessly as the last item
        return node

    def case_expr(self, items: list) -> CaseNode:
        node = CaseNode()
        it = iter(items)
        next(it)  # CASE token
        base_expr = self._to_node(next(it))

        for token in it:
            keyword = str(token).upper()
            if keyword == "WHEN":
                compare_val = self._to_node(next(it))
                next(it)  # THEN token
                result_val = self._to_node(next(it))
                # Expand simple CASE into searched CASE: WHEN base = val THEN result
                cond = ComparisonNode(left=base_expr, op="=", right=compare_val)
                node.conditions.append((cond, result_val))
            elif keyword == "ELSE":
                node.else_result = self._to_node(next(it))
        return node


# ==========================================
# 4. TRANSLATE FUNCTION
# ==========================================

# Parser is now stateless; transformer carries per-call state
_PARSER = Lark(TABLEAU_GRAMMAR, parser="lalr")

_NBSP = "\xa0"


def translate(
    expression: str,
    parameter_names: frozenset[str] = frozenset(),
) -> str:
    """Translate a Tableau formula to Presto SQL.

    Args:
        expression: A Tableau calculated field formula string.
        parameter_names: Raw names of Tableau parameters as they appear between
            brackets in the formula — e.g. frozenset({"Min Date", "Max Price"}).
            Matched references render as a placeholder instead of a field.

    Raises:
        ValueError: If the expression cannot be parsed.
    """
    clean = expression.replace(_NBSP, " ").strip()
    try:
        tree = _PARSER.parse(clean)
    except Exception as e:
        raise ValueError(f"Failed to parse Tableau expression: {e}") from e
    result = TableauToPrestoNodes(parameter_names).transform(tree)
    return result.render() if isinstance(result, SqlNode) else str(result)
