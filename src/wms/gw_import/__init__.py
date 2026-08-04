from __future__ import annotations

import inspect

from . import aos_inventory, warhammer_40k
from .common import normalized_lines, parse_unit_rows
from .models import GWImportError, ImportedPhysicalModel, ImportedUnit, ParsedArmy


PARSERS = (aos_inventory, warhammer_40k)


def _rule_size(lookup, system: str, faction: str, unit: str):
    """Accept the new three-argument lookup and legacy two-argument callbacks."""
    try:
        first = next(iter(inspect.signature(lookup).parameters.values())).name
        if first in {"faction", "faction_name"}:
            return lookup(faction, unit, system)
    except (TypeError, ValueError, StopIteration):
        pass
    try:
        return lookup(system, faction, unit)
    except TypeError:
        return lookup(faction, unit)


def parse_gw_army_text(text: str, unit_size_lookup=None, fallback_game_system="", fallback_faction="",
                       rule_parser_type="", parser_definition=None) -> ParsedArmy:
    """Choose the parser that matches the pasted GW export text."""
    if not text.strip():
        raise GWImportError("Paste GW App exported text first.")
    parser_by_type = {
        "gw_app_aos_inventory": aos_inventory,
        "gw_app_40k": warhammer_40k,
    }
    if rule_parser_type == "generic_unit_rows":
        units = parse_unit_rows(normalized_lines(text))
        if unit_size_lookup:
            units = tuple(ImportedUnit(
                unit.name,
                _rule_size(
                    unit_size_lookup, fallback_game_system, fallback_faction, unit.name
                ) or unit.model_count,
            ) for unit in units)
        return ParsedArmy(
            fallback_faction.strip(), units, fallback_game_system.strip(),
            (), "generic_unit_rows",
        )
    forced = parser_by_type.get(rule_parser_type)
    candidates = (forced,) if forced else PARSERS
    for parser in candidates:
        if parser is None:
            continue
        if parser.matches(text) or (forced is parser and parser_definition):
            army = parser.parse(text, parser_definition)
            if unit_size_lookup:
                resolved = []
                missing = []
                for unit in army.units:
                    rule_count = _rule_size(unit_size_lookup, army.game_system, army.faction, unit.name)
                    if rule_count is None:
                        missing.append(unit.name)
                        resolved.append(unit)
                    else:
                        resolved.append(ImportedUnit(
                            unit.name, rule_count, unit.physical_models, unit.points
                        ))
                units = tuple(resolved)
                return ParsedArmy(
                    army.faction, units, army.game_system,
                    tuple(dict.fromkeys(missing)), army.detected_format,
                )
            return army
    if rule_parser_type:
        raise GWImportError(
            f"The selected Parser '{rule_parser_type}' could not recognize this App Text. "
            "Check that the export belongs to this Game System or replace/edit its Parser Rule in Settings."
        )
    if fallback_game_system.strip() and fallback_faction.strip():
        units = parse_unit_rows(normalized_lines(text))
        if unit_size_lookup:
            units = tuple(ImportedUnit(
                unit.name,
                _rule_size(unit_size_lookup, fallback_game_system, fallback_faction, unit.name) or unit.model_count,
            ) for unit in units)
        return ParsedArmy(fallback_faction.strip(), units, fallback_game_system.strip())
    expected = f"the {rule_parser_type} format" if rule_parser_type else "an Inventory title or a Faction line"
    raise GWImportError(f"This export does not match {expected}.")


__all__ = ["GWImportError", "ImportedPhysicalModel", "ImportedUnit", "ParsedArmy", "parse_gw_army_text"]
