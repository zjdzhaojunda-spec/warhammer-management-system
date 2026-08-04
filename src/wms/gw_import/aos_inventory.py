from __future__ import annotations

import re

from .common import normalized_lines, parse_unit_rows
from .models import GWImportError, ParsedArmy


INVENTORY_TITLE_PATTERN = re.compile(
    r"^\s*.+?\s+Inventory\s+\d+\s*/\s*\d+\s*(?:points?|pts?)\s*$",
    re.IGNORECASE,
)


def matches(text: str) -> bool:
    return any(INVENTORY_TITLE_PATTERN.match(line) for line in text.splitlines())


def parse(text: str, definition=None) -> ParsedArmy:
    lines = normalized_lines(text)
    configured = (definition or {}).get("patterns", {})
    inventory_pattern = re.compile(
        configured.get("inventory", INVENTORY_TITLE_PATTERN.pattern), re.IGNORECASE
    )
    unit_pattern = re.compile(configured["unit"], re.IGNORECASE) if configured.get("unit") else None
    item_pattern = re.compile(configured["counted_item"], re.IGNORECASE) if configured.get("counted_item") else None
    faction = ""
    for index, line in enumerate(lines):
        if inventory_pattern.match(line):
            faction = next((candidate.strip() for candidate in lines[index + 1:] if candidate.strip()), "")
            break
    if not faction:
        raise GWImportError("Could not find the faction below the Inventory title.")
    return ParsedArmy(
        game_system="Age of Sigmar",
        faction=faction,
        units=parse_unit_rows(lines, unit_pattern, item_pattern),
    )
