"""Replace Tableau calculated-field references with direct-field references in a .twb XML workbook.

Design goals: safe (validate first, never mutate in place), maintainable
(one module, one job), observable (structured report of every action).
"""

import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree


# ==========================================
# 1. DATA STRUCTURES
# ==========================================


@dataclass
class ReplacementReport:
    """Structured record of what the replacer did, skipped, and flagged."""

    replaced: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    unmapped: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"replaced={len(self.replaced)} "
            f"skipped={len(self.skipped)} "
            f"unmapped={len(self.unmapped)} "
            f"warnings={len(self.warnings)}"
        )


# ==========================================
# 2. VALIDATION
# ==========================================


# Calcs containing these tokens are row-incompatible with a direct field swap.
_UNSAFE_TOKENS = re.compile(
    r"\b(SUM|AVG|MIN|MAX|COUNT|COUNTD|MEDIAN|PERCENTILE|STDEV|VAR|"
    r"WINDOW_|RUNNING_|FIRST|LAST|INDEX|RANK|LOOKUP|PREVIOUS_VALUE|"
    r"TOTAL|ATTR)\s*\(",
    re.IGNORECASE,
)
_LOD_TOKEN = re.compile(r"\{\s*(FIXED|INCLUDE|EXCLUDE)\b", re.IGNORECASE)


def _is_row_safe(formula: str) -> tuple[bool, str | None]:
    """Return (safe, reason). A calc is row-safe if no aggregation/LOD/table calc."""
    if _LOD_TOKEN.search(formula):
        return False, "contains LOD expression"
    if _UNSAFE_TOKENS.search(formula):
        return False, "contains aggregation or table calc"
    return True, None


def _find_calculation_nodes(tree: etree._ElementTree) -> dict[str, etree._Element]:
    """Map internal calc name → the <column> element that defines it.

    Tableau represents calculated fields as <column> elements with a child
    <calculation class='tableau' formula='...'/>. The column's 'name'
    attribute is the bracketed internal id (e.g. '[Calculation_123]').
    """
    nodes: dict[str, etree._Element] = {}
    for col in tree.iter("column"):
        calc = col.find("calculation")
        if calc is not None and calc.get("class") == "tableau":
            name = col.get("name")
            if name:
                nodes[name] = col
    return nodes


def validate_mapping(
    tree: etree._ElementTree,
    mapping: dict[str, str],
) -> ReplacementReport:
    """Pre-flight check. Populates skipped/unmapped/warnings; never mutates the tree."""
    report = ReplacementReport()
    calc_nodes = _find_calculation_nodes(tree)

    for internal_name, direct_field in mapping.items():
        if internal_name not in calc_nodes:
            report.unmapped.append({
                "internal_name": internal_name,
                "direct_field": direct_field,
                "reason": "not found in workbook",
            })
            continue

        calc = calc_nodes[internal_name].find("calculation")
        formula = calc.get("formula", "")
        safe, reason = _is_row_safe(formula)
        if not safe:
            report.skipped.append({
                "internal_name": internal_name,
                "direct_field": direct_field,
                "reason": reason,
                "formula": formula,
            })

    return report


# ==========================================
# 3. REPLACEMENT
# ==========================================


# XML attributes that can contain bracketed field references.
# - level / expression: <groupfilter> and <link> bindings (filters/slicers, drill-through)
_REF_ATTRIBUTES = ("name", "column", "field", "formula", "value", "level", "expression")


def _build_substitution_pattern(mapping: dict[str, str]) -> re.Pattern:
    """Length-sorted regex so '[Calc_10]' matches before '[Calc_1]'."""
    keys = sorted(mapping.keys(), key=len, reverse=True)
    return re.compile("|".join(re.escape(k) for k in keys))


def _substitute_in_text(
    text: str,
    mapping: dict[str, str],
    pattern: re.Pattern,
) -> tuple[str, list[str]]:
    """Substitute and return (new_text, list_of_replaced_keys)."""
    replaced: list[str] = []

    def _sub(match: re.Match) -> str:
        key = match.group(0)
        replaced.append(key)
        return f"[{mapping[key]}]"

    return pattern.sub(_sub, text), replaced


def _apply_replacements(
    tree: etree._ElementTree,
    mapping: dict[str, str],
    skipped_keys: set[str],
    report: ReplacementReport,
) -> None:
    """Walk the tree once, rewriting any attribute or text node that references a mapped calc.

    The defining <column> element for a replaced calc is removed at the end —
    nothing should reference it anymore, and leaving it would let Tableau
    recompute it on load.
    """
    active_map = {k: v for k, v in mapping.items() if k not in skipped_keys}
    if not active_map:
        return

    pattern = _build_substitution_pattern(active_map)
    calc_nodes = _find_calculation_nodes(tree)
    all_replaced: set[str] = set()

    for elem in tree.iter():
        # attributes
        for attr in _REF_ATTRIBUTES:
            val = elem.get(attr)
            if val and "[" in val:
                new_val, hits = _substitute_in_text(val, active_map, pattern)
                if hits:
                    elem.set(attr, new_val)
                    for key in hits:
                        all_replaced.add(key)
                        report.replaced.append({
                            "internal_name": key,
                            "direct_field": active_map[key],
                            "location": f"<{elem.tag} {attr}=...>",
                        })

        # text content (rare for refs, but covers <formula>...</formula> style)
        if elem.text and "[" in elem.text:
            new_text, hits = _substitute_in_text(elem.text, active_map, pattern)
            if hits:
                elem.text = new_text
                for key in hits:
                    all_replaced.add(key)
                    report.replaced.append({
                        "internal_name": key,
                        "direct_field": active_map[key],
                        "location": f"<{elem.tag}> text",
                    })

    # Convert each replaced calc's <calculation> into a passthrough alias of the
    # direct field. The <column> definition stays intact so filters, slicers,
    # column-instances, and links remain bound. Engine cost collapses to a
    # column rename. Use vacuum_workbook() afterward to remove orphans if desired.
    for key in all_replaced:
        node = calc_nodes.get(key)
        if node is None:
            continue
        calc_child = node.find("calculation")
        if calc_child is not None:
            calc_child.set("formula", f"[{active_map[key]}]")


# ==========================================
# 4. PUBLIC API
# ==========================================


def _detect_format(path: Path, format: str | None) -> str:
    """Resolve format from arg or path suffix. Default to 'twb'."""
    if format is not None:
        if format not in ("twb", "twbx"):
            raise ValueError(f"format must be 'twb' or 'twbx', got {format!r}")
        return format
    suffix = path.suffix.lower().lstrip(".")
    return suffix if suffix in ("twb", "twbx") else "twb"


def _read_workbook(path: Path, format: str) -> tuple[etree._ElementTree, str | None]:
    """Return (parsed tree, inner_twb_name). inner_twb_name is None for raw .twb."""
    parser = etree.XMLParser(remove_blank_text=False, strip_cdata=False)
    if format == "twb":
        return etree.parse(str(path), parser), None
    with zipfile.ZipFile(path, "r") as zf:
        twb_names = [n for n in zf.namelist() if n.lower().endswith(".twb")]
        if not twb_names:
            raise ValueError(f"No .twb file found inside {path}")
        if len(twb_names) > 1:
            raise ValueError(f"Multiple .twb files inside {path}: {twb_names}")
        inner = twb_names[0]
        with zf.open(inner) as f:
            return etree.parse(f, parser), inner


def _write_workbook(
    tree: etree._ElementTree,
    output_path: Path,
    format: str,
    source_twbx: Path | None,
    inner_twb_name: str | None,
) -> None:
    """Write a .twb (raw XML) or .twbx (zip with rewritten inner .twb)."""
    if format == "twb":
        tree.write(
            str(output_path),
            xml_declaration=True,
            encoding="utf-8",
            standalone=True,
        )
        return

    if source_twbx is None or inner_twb_name is None:
        raise ValueError("twbx output requires the source twbx for repacking")

    new_xml = etree.tostring(
        tree, xml_declaration=True, encoding="utf-8", standalone=True
    )
    with zipfile.ZipFile(source_twbx, "r") as zin, \
         zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = new_xml if item.filename == inner_twb_name else zin.read(item.filename)
            zout.writestr(item, data)


def replace_in_workbook(
    input_path: str | Path,
    output_path: str | Path,
    mapping: dict[str, str],
    dry_run: bool = False,
    format: str | None = None,
) -> ReplacementReport:
    """Replace calculated-field references with direct-field references in a workbook.

    Args:
        input_path: Path to the source .twb or .twbx (never modified).
        output_path: Path where the rewritten workbook is written. Ignored if dry_run=True.
            Should use the same extension as the input format.
        mapping: {internal_calc_name: direct_field_name}.
            Keys must include the brackets, e.g. '[Calculation_123]'.
            Values are bare column names, e.g. 'auto_lead_source'.
        dry_run: If True, validate and report without writing any file.
        format: 'twb' or 'twbx'. Defaults to inferring from input_path suffix
            (with 'twb' as the fallback).

    Returns:
        ReplacementReport with replaced/skipped/unmapped/warnings.
    """
    input_path = Path(input_path)
    fmt = _detect_format(input_path, format)
    tree, inner = _read_workbook(input_path, fmt)

    report = validate_mapping(tree, mapping)
    skipped_keys = {item["internal_name"] for item in report.skipped}
    unmapped_keys = {item["internal_name"] for item in report.unmapped}
    inactive = skipped_keys | unmapped_keys

    _apply_replacements(tree, mapping, inactive, report)

    if not dry_run:
        output_path = Path(output_path)
        backup = output_path.with_suffix(output_path.suffix + ".orig")
        if not backup.exists():
            shutil.copy2(input_path, backup)
        _write_workbook(tree, output_path, fmt, input_path, inner)

    return report


# ==========================================
# 5. VACUUM (OPT-IN CLEANUP)
# ==========================================


def vacuum_workbook(
    input_path: str | Path,
    output_path: str | Path,
    calc_ids: list[str],
    dry_run: bool = False,
    format: str | None = None,
) -> ReplacementReport:
    """Remove <column> definitions for calcs that no longer have any references.

    Intended to run AFTER replace_in_workbook, once you've confirmed the
    workbook still opens correctly. Safe by construction: a column is only
    removed when zero references to its internal_name remain anywhere outside
    its own <column> subtree.

    Args:
        input_path: Source .twb or .twbx (typically the output of replace_in_workbook).
        output_path: Where to write the cleaned workbook (matching format).
        calc_ids: Internal names (with brackets) of calcs that are candidates
            for removal — typically the keys you passed to replace_in_workbook.
        dry_run: If True, report what would be removed without writing.
        format: 'twb' or 'twbx'. Defaults to inferring from input_path suffix.

    Returns:
        ReplacementReport. `replaced` lists the removed columns;
        `skipped` lists columns kept because references still exist.
    """
    input_path = Path(input_path)
    fmt = _detect_format(input_path, format)
    tree, inner = _read_workbook(input_path, fmt)

    report = ReplacementReport()
    calc_nodes = _find_calculation_nodes(tree)

    for calc_id in calc_ids:
        node = calc_nodes.get(calc_id)
        if node is None:
            continue  # already gone — nothing to do
        if _has_external_references(tree, node, calc_id):
            report.skipped.append({
                "internal_name": calc_id,
                "reason": "external references still exist",
            })
            continue
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)
            report.replaced.append({
                "internal_name": calc_id,
                "action": "removed orphan <column> definition",
            })

    if not dry_run:
        output_path = Path(output_path)
        backup = output_path.with_suffix(output_path.suffix + ".orig")
        if not backup.exists():
            shutil.copy2(input_path, backup)
        _write_workbook(tree, output_path, fmt, input_path, inner)

    return report


def _has_external_references(
    tree: etree._ElementTree,
    own_node: etree._Element,
    calc_id: str,
) -> bool:
    """True if calc_id appears anywhere outside its own <column> subtree."""
    own_descendants = set(own_node.iter())
    encoded = calc_id.replace("[", "%5B").replace("]", "%5D")
    for elem in tree.iter():
        if elem in own_descendants:
            continue
        for val in elem.attrib.values():
            if calc_id in val or encoded in val:
                return True
        if elem.text and (calc_id in elem.text or encoded in elem.text):
            return True
    return False
