from __future__ import annotations

import re

from .models import GWImportError, ImportedUnit


UNIT_PATTERN = re.compile(
    r"^\s*(?:(\d+)x\s+)?(.+?)\s*\(\d+\s*(?:points?|pts?)?\)\s*$",
    re.IGNORECASE,
)
MODEL_PATTERN = re.compile(r"^(\s*)[•\-]\s*(\d+)x\s+(.+?)\s*$", re.IGNORECASE)


def normalized_lines(text: str) -> list[str]:
    if not text.strip():
        raise GWImportError("Paste GW App exported text first.")
    return text.replace("\r\n", "\n").replace("\r", "\n").splitlines()


def parse_unit_rows(lines: list[str], unit_pattern=None, model_pattern=None) -> tuple[ImportedUnit, ...]:
    unit_pattern = unit_pattern or UNIT_PATTERN
    model_pattern = model_pattern or MODEL_PATTERN
    entries: list[tuple[str, int, int]] = []
    for index, line in enumerate(lines):
        match = unit_pattern.match(line)
        if not match:
            continue
        groups = match.groups()
        prefix_count, name = groups[0], groups[1]
        if name.strip().startswith("+"):
            continue
        entries.append((name.strip(), int(prefix_count or 0), index))

    if not entries:
        raise GWImportError("Could not find units written like 'Unit Name (100)' or 'Unit Name (100 Points)'.")

    units: list[ImportedUnit] = []
    for position, (name, prefix_count, start) in enumerate(entries):
        end = entries[position + 1][2] if position + 1 < len(entries) else len(lines)
        model_rows: list[tuple[int, int]] = []
        for line in lines[start + 1:end]:
            match = model_pattern.match(line)
            if match:
                indent, count, _ = match.groups()
                model_rows.append((len(indent.expandtabs(4)), int(count)))
        if prefix_count:
            count = prefix_count
        elif model_rows:
            shallowest = min(indent for indent, _ in model_rows)
            count = sum(amount for indent, amount in model_rows if indent == shallowest)
        else:
            count = 1
        units.append(ImportedUnit(name=name, model_count=max(1, count)))
    return tuple(units)
