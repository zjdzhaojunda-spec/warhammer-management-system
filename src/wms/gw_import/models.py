from __future__ import annotations

from dataclasses import dataclass, field


class GWImportError(ValueError):
    """Raised when exported GW army text cannot be understood safely."""


@dataclass(frozen=True)
class ImportedPhysicalModel:
    """One kind of physical model parsed from an army export."""

    name: str
    quantity: int = 1
    weapons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImportedUnit:
    name: str
    model_count: int
    physical_models: tuple[ImportedPhysicalModel, ...] = field(default=(), compare=False)
    points: int | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ParsedArmy:
    faction: str
    units: tuple[ImportedUnit, ...]
    game_system: str = ""
    missing_profiles: tuple[str, ...] = ()
    detected_format: str = ""
