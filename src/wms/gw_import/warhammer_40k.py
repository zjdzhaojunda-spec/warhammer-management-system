from __future__ import annotations

import re

from .common import MODEL_PATTERN, UNIT_PATTERN, normalized_lines
from .models import GWImportError, ImportedPhysicalModel, ImportedUnit, ParsedArmy


FACTION_PATTERN = re.compile(r"^\s*\+?\s*FACTION(?:\s+KEYWORD)?\s*:\s*(.+?)\s*$", re.IGNORECASE)
INVENTORY_PATTERN = re.compile(r"^\s*(.+?)\s+Inventory\s*\(\s*\d+\s+points?\s*\)\s*$", re.IGNORECASE)
APP_VERSION_PATTERN = re.compile(r"^\s*Exported with App Version:", re.IGNORECASE)


def matches(text: str) -> bool:
    lines = text.splitlines()
    return any(FACTION_PATTERN.match(line) for line in lines) or (
        any(INVENTORY_PATTERN.match(line) for line in lines)
        and any(APP_VERSION_PATTERN.match(line) for line in lines)
    )


def _faction(lines: list[str]) -> str:
    labelled = next(
        (match.group(1).strip(" +") for line in lines if (match := FACTION_PATTERN.match(line))),
        "",
    )
    if labelled:
        return labelled
    for index, line in enumerate(lines):
        if INVENTORY_PATTERN.match(line):
            return next((candidate.strip() for candidate in lines[index + 1:] if candidate.strip()), "")
    return ""


def _patterns(definition=None):
    configured = (definition or {}).get("patterns", {})
    return (
        re.compile(configured.get("faction", FACTION_PATTERN.pattern), re.IGNORECASE),
        re.compile(configured.get("inventory", INVENTORY_PATTERN.pattern), re.IGNORECASE),
        re.compile(configured.get("unit", UNIT_PATTERN.pattern), re.IGNORECASE),
        re.compile(configured.get(
            "counted_item", r"^(\s*)(?:[•\-]\s*)?(\d+)x\s+(.+?)\s*$"
        ), re.IGNORECASE),
    )


def _unit_parts(match, line):
    groups = match.groups()
    prefix, name = groups[0], groups[1]
    points = int(groups[2]) if len(groups) >= 3 and groups[2] else int(
        re.search(r"\((\d+)\s*(?:points?|pts?)?\)", line, re.IGNORECASE).group(1)
    )
    return prefix, name, points


def parse_export_entries(text: str, definition=None) -> tuple[tuple[str, int, int], ...]:
    """Return 40K unit name, physical-model count, and points from an App export."""
    lines = normalized_lines(text)
    _, inventory_pattern, unit_pattern, model_pattern = _patterns(definition)
    entries: list[tuple[str, int, int, int]] = []
    for index, line in enumerate(lines):
        match = unit_pattern.match(line)
        if not match:
            continue
        prefix_count, name, parsed_points = _unit_parts(match, line)
        clean_name = name.strip()
        if (clean_name.startswith("+") or INVENTORY_PATTERN.match(line)
                or clean_name.casefold() in {"combat patrol", "incursion", "strike force", "onslaught"}):
            continue
        entries.append((clean_name, int(prefix_count or 0), parsed_points, index))
    if not entries:
        raise GWImportError("Could not find 40K units written like 'Unit Name (100 points)'.")

    result: list[tuple[str, int, int]] = []
    for position, (name, prefix_count, points, start) in enumerate(entries):
        end = entries[position + 1][3] if position + 1 < len(entries) else len(lines)
        model_rows: list[tuple[int, int]] = []
        for line in lines[start + 1:end]:
            match = model_pattern.match(line)
            if match:
                indent, count, _ = match.groups()
                model_rows.append((len(indent.expandtabs(4)), int(count)))
        if prefix_count:
            count = prefix_count
        elif model_rows and len({indent for indent, _ in model_rows}) > 1:
            shallowest = min(indent for indent, _ in model_rows)
            count = sum(amount for indent, amount in model_rows if indent == shallowest)
        else:
            # A single-depth bullet list is the wargear carried by one model.
            count = 1
        result.append((name, max(1, count), points))
    return tuple(result)


def parse(text: str, definition=None) -> ParsedArmy:
    lines = normalized_lines(text)
    logic = (definition or {}).get("logic", {})
    faction_pattern, inventory_pattern, unit_pattern, model_pattern = _patterns(definition)
    faction = next((m.group(1).strip(" +") for line in lines
                    if (m := faction_pattern.match(line))), "")
    if not faction and logic.get("faction_detection", "label_or_line_after_inventory") != "label_only":
        for index, line in enumerate(lines):
            if inventory_pattern.match(line):
                faction = next((candidate.strip() for candidate in lines[index + 1:] if candidate.strip()), "")
                break
    if not faction:
        raise GWImportError("Could not find a 'Faction' or 'Faction Keyword' line.")
    entries = parse_export_entries(text, definition)
    lines = normalized_lines(text)
    unit_rows = []
    for index, line in enumerate(lines):
        match = unit_pattern.match(line)
        if match and any(_unit_parts(match, line)[1].strip() == name for name, _, _ in entries):
            unit_rows.append((index, _unit_parts(match, line)[1].strip()))

    parsed_units = []
    for position, (start, name) in enumerate(unit_rows):
        end = unit_rows[position + 1][0] if position + 1 < len(unit_rows) else len(lines)
        points = next((p for n, _, p in entries if n == name), None)
        if logic.get("points_detection", "unit_heading") in {"none", "official_data"}:
            points = None
        block = lines[start + 1:end]
        rows = []
        for offset, line in enumerate(block):
            match = model_pattern.match(line)
            if match:
                indent, quantity, model_name = match.groups()
                rows.append((offset, len(indent.expandtabs(4)), int(quantity), model_name.strip()))
        models = []
        if logic.get("model_strategy") == "unit_name_single":
            rows = []
            models = [ImportedPhysicalModel(name)]
        elif rows:
            shallowest = min(row[1] for row in rows)
            model_rows = [row for row in rows if row[1] == shallowest]
            # Current 40K exports omit the model name for many single-model
            # characters and vehicles, listing wargear directly. A lone 1x row
            # whose label does not resemble the Unit is therefore a weapon row.
            if (len(model_rows) == 1 and model_rows[0][2] == 1
                    and name.casefold() not in model_rows[0][3].casefold()
                    and model_rows[0][3].casefold() not in name.casefold()):
                weapons = []
                for candidate in block:
                    weapon = model_pattern.match(candidate)
                    if weapon:
                        weapons.append(weapon.group(3).strip())
                if logic.get("weapon_assignment", "nested_under_model") != "nested_under_model":
                    weapons = []
                models.append(ImportedPhysicalModel(name, 1, tuple(dict.fromkeys(weapons))))
                model_rows = []
            for row_index, (offset, indent, quantity, model_name) in enumerate(model_rows):
                stop = model_rows[row_index + 1][0] if row_index + 1 < len(model_rows) else len(block)
                weapons = []
                for candidate in block[offset + 1:stop]:
                    weapon = model_pattern.match(candidate)
                    if weapon:
                        weapons.append(weapon.group(3).strip())
                if logic.get("weapon_assignment", "nested_under_model") != "nested_under_model":
                    weapons = []
                models.append(ImportedPhysicalModel(model_name, quantity, tuple(dict.fromkeys(weapons))))
        model_count = sum(model.quantity for model in models) if models else 1
        parsed_units.append(ImportedUnit(name, model_count, tuple(models), points))
    return ParsedArmy(faction, tuple(parsed_units), "Warhammer 40,000", (), "gw_40k_app")
