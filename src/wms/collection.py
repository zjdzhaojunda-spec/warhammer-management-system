from __future__ import annotations

import sqlite3
import re
import os
import tempfile
from pathlib import Path
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from .db import Database
from .gw_import import ImportedUnit


ASSEMBLY_STATUSES = frozenset({"unassembled", "partially_assembled", "assembled"})
PAINT_STATUSES = frozenset({"unpainted", "primed", "in_progress", "painted"})
_POINTS_UNCHANGED = object()


class CollectionError(ValueError):
    """Raised when a collection command violates a domain constraint."""


@dataclass(frozen=True)
class PhysicalModel:
    id: str
    unit_id: str
    reference_code: str
    display_name: str | None
    assembly_status: str
    paint_status: str
    is_magnetized: bool
    storage_location: str | None
    notes: str | None


@dataclass(frozen=True)
class ModelConfiguration:
    id: str
    physical_model_id: str
    name: str
    configuration_type: str
    rule_faction: str | None
    rule_model_name: str | None
    represented_unit_id: str | None
    represented_unit: str | None
    loadout_name: str | None
    points: int | None
    is_active: bool
    notes: str | None


@dataclass(frozen=True)
class CollectionRow:
    id: str
    unit_id: str
    unit_code: str
    model_code: str
    game_system: str
    faction: str
    unit: str
    display_name: str | None
    assembly_status: str
    paint_status: str
    is_magnetized: bool
    storage_location: str | None
    notes: str | None
    current_model_configuration: str | None
    current_weapon_configuration: str | None
    represented_unit: str | None
    current_loadout: str | None
    current_points: int | None
    unit_points: int | None
    unit_points_manual: bool


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _clean_name(value: str, field: str = "name") -> str:
    cleaned = value.strip()
    if not cleaned:
        raise CollectionError(f"{field} must not be empty")
    return cleaned


def _reference_code(prefix: str, game_system: str, unit_name: str, entity_id: str) -> str:
    system = "".join(part[0] for part in re.findall(r"[A-Za-z0-9]+", game_system))[:6] or "SYS"
    unit = re.sub(r"[^A-Za-z0-9]+", "-", unit_name).strip("-")[:28] or "MODEL"
    suffix = entity_id.replace("-", "")[:6].upper()
    return f"{prefix}-{system.upper()}-{unit.upper()}-{suffix}"


class CollectionService:
    def __init__(self, database: Database):
        self.database = database

    def _create_named(self, table: str, parent_column: str | None, parent_id: str | None, name: str) -> str:
        clean = _clean_name(name)
        entity_id, timestamp = str(uuid4()), _now()
        columns = ["id", "name", "created_at", "updated_at"]
        values: list[str] = [entity_id, clean, timestamp, timestamp]
        if parent_column:
            columns.insert(1, parent_column)
            values.insert(1, str(parent_id))
        placeholders = ", ".join("?" for _ in values)
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                    values,
                )
        except sqlite3.IntegrityError as exc:
            raise CollectionError(f"Could not create {table}: {exc}") from exc
        return entity_id

    def create_game_system(self, name: str) -> str:
        return self._create_named("game_system", None, None, name)

    def create_faction(self, game_system_id: str, name: str) -> str:
        return self._create_named("faction", "game_system_id", game_system_id, name)

    def create_unit(self, faction_id: str, name: str) -> str:
        clean = _clean_name(name)
        entity_id, timestamp = str(uuid4()), _now()
        try:
            with self.database.transaction() as connection:
                system = connection.execute(
                    """SELECT game_system.name FROM faction
                         JOIN game_system ON game_system.id = faction.game_system_id
                        WHERE faction.id = ?""", (faction_id,),
                ).fetchone()
                if not system:
                    raise CollectionError("Faction does not exist")
                connection.execute(
                    """INSERT INTO unit
                       (id, faction_id, name, created_at, updated_at, reference_code)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (entity_id, faction_id, clean, timestamp, timestamp,
                     _reference_code("U", system["name"], clean, entity_id)),
                )
        except sqlite3.IntegrityError as exc:
            raise CollectionError(f"Could not create unit: {exc}") from exc
        return entity_id

    def create_model(
        self,
        unit_id: str,
        *,
        display_name: str | None = None,
        assembly_status: str = "unassembled",
        paint_status: str = "unpainted",
        is_magnetized: bool = False,
        storage_location: str | None = None,
        notes: str | None = None,
    ) -> str:
        self._validate_statuses(assembly_status, paint_status)
        model_id, timestamp = str(uuid4()), _now()
        try:
            with self.database.transaction() as connection:
                hierarchy = connection.execute(
                    """SELECT unit.name AS unit_name, game_system.name AS game_system
                         FROM unit JOIN faction ON faction.id = unit.faction_id
                         JOIN game_system ON game_system.id = faction.game_system_id
                        WHERE unit.id = ?""", (unit_id,),
                ).fetchone()
                if not hierarchy:
                    raise CollectionError("Unit does not exist")
                connection.execute(
                    """INSERT INTO physical_model
                    (id, unit_id, display_name, assembly_status, paint_status,
                     is_magnetized, storage_location, notes, created_at, updated_at,
                     reference_code)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        model_id,
                        unit_id,
                        display_name.strip() or None if display_name else None,
                        assembly_status,
                        paint_status,
                        int(is_magnetized),
                        storage_location,
                        notes,
                        timestamp,
                        timestamp,
                        _reference_code("M", hierarchy["game_system"], hierarchy["unit_name"], model_id),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise CollectionError(f"Could not create physical model: {exc}") from exc
        return model_id

    def create_configuration(
        self, model_id: str, name: str, *, represented_unit_id: str | None = None,
        loadout_name: str | None = None, points: int | None = None,
        is_active: bool = True, notes: str | None = None,
        configuration_type: str | None = None, rule_faction: str | None = None,
        rule_model_name: str | None = None,
        unit_points: int | None | object = _POINTS_UNCHANGED,
    ) -> str:
        clean = _clean_name(name, "configuration state")
        kind = (configuration_type or ("model" if represented_unit_id else "weapon")).strip().casefold()
        if kind not in {"model", "weapon"}:
            raise CollectionError("Configuration type must be Model or Weapon")
        clean_rule_faction = rule_faction.strip() or None if rule_faction else None
        clean_rule_model = rule_model_name.strip() or None if rule_model_name else None
        clean_loadout = loadout_name.strip() or None if loadout_name else None
        if kind == "model" and not (clean_rule_model or represented_unit_id):
            raise CollectionError("Choose a model from the current Game System rule file")
        if kind == "model" and clean_loadout:
            raise CollectionError("A Model configuration cannot also contain a Weapon / Loadout")
        if kind == "weapon" and not clean_loadout:
            raise CollectionError("Enter a Weapon / Loadout")
        if kind == "weapon" and (clean_rule_model or represented_unit_id):
            raise CollectionError("A Weapon configuration cannot also represent a model")
        if points is not None and points < 0:
            raise CollectionError("Points must be zero or greater")
        configuration_id, timestamp = str(uuid4()), _now()
        try:
            with self.database.transaction() as connection:
                model = connection.execute(
                    "SELECT unit_id FROM physical_model WHERE id = ?", (model_id,)
                ).fetchone()
                if not model:
                    raise CollectionError("Physical model does not exist")
                if represented_unit_id:
                    compatible = connection.execute(
                        """SELECT 1 FROM unit AS target
                           JOIN faction AS target_faction ON target_faction.id = target.faction_id
                           JOIN unit AS owned ON owned.id = ?
                           JOIN faction AS owned_faction ON owned_faction.id = owned.faction_id
                           WHERE target.id = ?
                             AND target_faction.game_system_id = owned_faction.game_system_id""",
                        (model["unit_id"], represented_unit_id),
                    ).fetchone()
                    if not compatible:
                        raise CollectionError(
                            "A configuration can only represent a Unit in the same Game System"
                        )
                if is_active:
                    connection.execute(
                        """UPDATE configuration SET is_active = 0, updated_at = ?
                            WHERE physical_model_id = ? AND configuration_type = ?""",
                        (timestamp, model_id, kind),
                    )
                connection.execute(
                    """INSERT INTO configuration
                       (id, physical_model_id, name, represented_unit_id, loadout_name,
                        points, is_active, notes, created_at, updated_at,
                        configuration_type, rule_faction, rule_model_name)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (configuration_id, model_id, clean, represented_unit_id, clean_loadout,
                     points, int(is_active), notes.strip() or None if notes else None,
                     timestamp, timestamp, kind, clean_rule_faction, clean_rule_model),
                )
                if is_active and kind == "model" and unit_points is not _POINTS_UNCHANGED:
                    connection.execute(
                        "UPDATE unit SET points = ?, points_manual = 0, updated_at = ? WHERE id = ?",
                        (unit_points, timestamp, model["unit_id"]),
                    )
        except sqlite3.IntegrityError as exc:
            raise CollectionError(f"Could not create configuration: {exc}") from exc
        return configuration_id

    def list_configurations(self, model_id: str) -> list[ModelConfiguration]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT configuration.id, configuration.physical_model_id,
                          configuration.name, configuration.configuration_type,
                          configuration.rule_faction, configuration.rule_model_name,
                          configuration.represented_unit_id,
                          represented.name AS represented_unit, configuration.loadout_name,
                          configuration.points, configuration.is_active, configuration.notes
                   FROM configuration
                   LEFT JOIN unit AS represented ON represented.id = configuration.represented_unit_id
                   WHERE configuration.physical_model_id = ?
                   ORDER BY configuration.name COLLATE NOCASE""",
                (model_id,),
            ).fetchall()
        return [ModelConfiguration(**dict(row) | {"is_active": bool(row["is_active"])}) for row in rows]

    def list_compatible_units(self, model_id: str) -> list[tuple[str, str, str]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT target.id, target_faction.name AS faction, target.name
                   FROM physical_model AS model
                   JOIN unit AS owned ON owned.id = model.unit_id
                   JOIN faction AS owned_faction ON owned_faction.id = owned.faction_id
                   JOIN faction AS target_faction
                     ON target_faction.game_system_id = owned_faction.game_system_id
                   JOIN unit AS target ON target.faction_id = target_faction.id
                   WHERE model.id = ?
                   ORDER BY target_faction.name COLLATE NOCASE, target.name COLLATE NOCASE""",
                (model_id,),
            ).fetchall()
        return [(row["id"], row["faction"], row["name"]) for row in rows]

    def game_system_for_model(self, model_id: str) -> str:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT system.name
                     FROM physical_model AS model
                     JOIN unit ON unit.id = model.unit_id
                     JOIN faction ON faction.id = unit.faction_id
                     JOIN game_system AS system ON system.id = faction.game_system_id
                    WHERE model.id = ?""",
                (model_id,),
            ).fetchone()
        if not row:
            raise CollectionError("Physical model does not exist")
        return str(row["name"])

    def set_active_configuration(
        self, model_id: str, configuration_id: str | None,
        *, unit_points: int | None | object = _POINTS_UNCHANGED,
    ) -> None:
        timestamp = _now()
        with self.database.transaction() as connection:
            if not connection.execute(
                "SELECT 1 FROM physical_model WHERE id = ?", (model_id,)
            ).fetchone():
                raise CollectionError("Physical model does not exist")
            selected = connection.execute(
                "SELECT configuration_type FROM configuration WHERE id = ? AND physical_model_id = ?",
                (configuration_id, model_id),
            ).fetchone() if configuration_id else None
            if configuration_id and not selected:
                raise CollectionError("Configuration does not belong to this physical model")
            if not configuration_id:
                connection.execute(
                    "UPDATE configuration SET is_active = 0, updated_at = ? WHERE physical_model_id = ?",
                    (timestamp, model_id),
                )
            if configuration_id:
                connection.execute(
                    """UPDATE configuration SET is_active = 0, updated_at = ?
                        WHERE physical_model_id = ? AND configuration_type = ?""",
                    (timestamp, model_id, selected["configuration_type"]),
                )
                connection.execute(
                    "UPDATE configuration SET is_active = 1, updated_at = ? WHERE id = ?",
                    (timestamp, configuration_id),
                )
            if unit_points is not _POINTS_UNCHANGED:
                connection.execute(
                    """UPDATE unit SET points = ?, points_manual = 0, updated_at = ?
                        WHERE id = (SELECT unit_id FROM physical_model WHERE id = ?)""",
                    (unit_points, timestamp, model_id),
                )

    def set_unit_points(self, unit_id: str, points: int | None, *, manual: bool) -> None:
        """Persist the Unit Instance score and whether it was explicitly overridden."""
        if points is not None and points < 0:
            raise CollectionError("Points must be zero or greater")
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """UPDATE unit
                      SET points = ?, points_manual = ?, updated_at = ?
                    WHERE id = ?""",
                (points, int(manual), _now(), unit_id),
            )
            if cursor.rowcount != 1:
                raise CollectionError("Unit does not exist")

    def clear_active_configuration(
        self, model_id: str, configuration_type: str,
        *, unit_points: int | None | object = _POINTS_UNCHANGED,
    ) -> None:
        kind = configuration_type.strip().casefold()
        if kind not in {"model", "weapon"}:
            raise CollectionError("Configuration type must be Model or Weapon")
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE configuration SET is_active = 0, updated_at = ?
                    WHERE physical_model_id = ? AND configuration_type = ?""",
                (_now(), model_id, kind),
            )
            if kind == "model" and unit_points is not _POINTS_UNCHANGED:
                connection.execute(
                    """UPDATE unit SET points = ?, points_manual = 0, updated_at = ?
                        WHERE id = (SELECT unit_id FROM physical_model WHERE id = ?)""",
                    (unit_points, _now(), model_id),
                )

    def delete_configuration(self, model_id: str, configuration_id: str) -> None:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM configuration WHERE id = ? AND physical_model_id = ?",
                (configuration_id, model_id),
            )
            if cursor.rowcount != 1:
                raise CollectionError("Configuration does not belong to this physical model")

    def copy_models(self, model_ids: list[str]) -> int:
        """Copy physical models into their existing units using new database IDs."""
        unique_ids = list(dict.fromkeys(model_ids))
        if not unique_ids:
            raise CollectionError("Select at least one physical model")
        timestamp, copied = _now(), 0
        with self.database.transaction() as connection:
            for model_id in unique_ids:
                model = connection.execute(
                    "SELECT * FROM physical_model WHERE id = ?", (model_id,)
                ).fetchone()
                if not model:
                    raise CollectionError("A selected physical model no longer exists")
                new_id = str(uuid4())
                hierarchy = connection.execute(
                    """SELECT unit.name AS unit_name, game_system.name AS game_system
                         FROM unit JOIN faction ON faction.id = unit.faction_id
                         JOIN game_system ON game_system.id = faction.game_system_id
                        WHERE unit.id = ?""", (model["unit_id"],),
                ).fetchone()
                connection.execute(
                    """INSERT INTO physical_model
                       (id, unit_id, display_name, assembly_status, paint_status,
                        is_magnetized, storage_location, notes, created_at, updated_at,
                        reference_code)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (new_id, model["unit_id"], model["display_name"], model["assembly_status"],
                     model["paint_status"], model["is_magnetized"], model["storage_location"],
                     model["notes"], timestamp, timestamp,
                     _reference_code("M", hierarchy["game_system"], hierarchy["unit_name"], new_id)),
                )
                self._copy_configurations(connection, model_id, new_id, timestamp)
                copied += 1
        return copied

    def copy_units(self, unit_ids: list[str]) -> tuple[int, int]:
        """Copy whole units and their models as independent Unit database entries."""
        unique_ids = list(dict.fromkeys(unit_ids))
        if not unique_ids:
            raise CollectionError("Select at least one unit")
        timestamp, model_count = _now(), 0
        with self.database.transaction() as connection:
            for unit_id in unique_ids:
                unit = connection.execute("SELECT * FROM unit WHERE id = ?", (unit_id,)).fetchone()
                if not unit:
                    raise CollectionError("A selected unit no longer exists")
                new_unit_id = str(uuid4())
                system = connection.execute(
                    """SELECT game_system.name FROM faction
                         JOIN game_system ON game_system.id = faction.game_system_id
                        WHERE faction.id = ?""", (unit["faction_id"],),
                ).fetchone()["name"]
                connection.execute(
                    """INSERT INTO unit
                       (id, faction_id, name, created_at, updated_at, reference_code, points, points_manual)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (new_unit_id, unit["faction_id"], unit["name"], timestamp, timestamp,
                     _reference_code("U", system, unit["name"], new_unit_id),
                     unit["points"], unit["points_manual"]),
                )
                models = connection.execute(
                    "SELECT * FROM physical_model WHERE unit_id = ? ORDER BY created_at, id", (unit_id,)
                ).fetchall()
                for model in models:
                    new_model_id = str(uuid4())
                    connection.execute(
                        """INSERT INTO physical_model
                           (id, unit_id, display_name, assembly_status, paint_status,
                            is_magnetized, storage_location, notes, created_at, updated_at,
                            reference_code)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (new_model_id, new_unit_id, model["display_name"], model["assembly_status"],
                         model["paint_status"], model["is_magnetized"], model["storage_location"],
                         model["notes"], timestamp, timestamp,
                         _reference_code("M", system, unit["name"], new_model_id)),
                    )
                    self._copy_configurations(
                        connection, model["id"], new_model_id, timestamp,
                        represented_unit_map={unit_id: new_unit_id},
                    )
                    model_count += 1
        return len(unique_ids), model_count

    def delete_units(self, unit_ids: list[str]) -> tuple[int, int]:
        """Delete complete Unit entries and their physical models in one transaction."""
        unique_ids = list(dict.fromkeys(unit_ids))
        if not unique_ids:
            raise CollectionError("Select at least one unit")
        with self.database.transaction() as connection:
            placeholders = ", ".join("?" for _ in unique_ids)
            rows = connection.execute(
                f"SELECT id FROM unit WHERE id IN ({placeholders})", unique_ids
            ).fetchall()
            if len(rows) != len(unique_ids):
                raise CollectionError("A selected unit no longer exists")
            model_count = int(connection.execute(
                f"SELECT COUNT(*) FROM physical_model WHERE unit_id IN ({placeholders})",
                unique_ids,
            ).fetchone()[0])
            connection.execute(f"DELETE FROM unit WHERE id IN ({placeholders})", unique_ids)
        return len(unique_ids), model_count

    @staticmethod
    def _copy_configurations(
        connection, source_model_id: str, target_model_id: str, timestamp: str,
        represented_unit_map: dict[str, str] | None = None,
    ) -> None:
        for configuration in connection.execute(
            """SELECT name, represented_unit_id, loadout_name, points, is_active, notes,
                      configuration_type, rule_faction, rule_model_name
               FROM configuration WHERE physical_model_id = ?""",
            (source_model_id,),
        ).fetchall():
            connection.execute(
                """INSERT INTO configuration
                   (id, physical_model_id, name, represented_unit_id, loadout_name,
                    points, is_active, notes, created_at, updated_at,
                    configuration_type, rule_faction, rule_model_name)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid4()), target_model_id, configuration["name"],
                 (represented_unit_map or {}).get(
                     configuration["represented_unit_id"], configuration["represented_unit_id"]
                 ), configuration["loadout_name"],
                 configuration["points"], configuration["is_active"],
                 configuration["notes"], timestamp, timestamp,
                 configuration["configuration_type"], configuration["rule_faction"],
                 configuration["rule_model_name"]),
            )

    def list_named(self, table: str, parent_column: str | None = None, parent_id: str | None = None) -> list[tuple[str, str]]:
        allowed = {
            "game_system": None,
            "faction": "game_system_id",
            "unit": "faction_id",
        }
        if table not in allowed or allowed[table] != parent_column:
            raise CollectionError("Unsupported collection hierarchy query")
        query = f"SELECT id, name FROM {table}"
        parameters: tuple[str, ...] = ()
        if parent_column:
            query += f" WHERE {parent_column} = ?"
            parameters = (str(parent_id),)
        query += " ORDER BY name COLLATE NOCASE"
        with self.database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [(row["id"], row["name"]) for row in rows]

    def list_models(self) -> list[PhysicalModel]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT id, unit_id, reference_code, display_name, assembly_status, paint_status,
                          is_magnetized, storage_location, notes
                   FROM physical_model ORDER BY created_at, id"""
            ).fetchall()
        return [PhysicalModel(**dict(row) | {"is_magnetized": bool(row["is_magnetized"])}) for row in rows]

    def list_collection(
        self,
        *,
        game_system: str | None = None,
        faction: str | None = None,
        unit: str | None = None,
        assembly_status: str | None = None,
        paint_status: str | None = None,
        is_magnetized: bool | None = None,
        search: str | None = None,
    ) -> list[CollectionRow]:
        clauses, parameters = [], []
        for column, value in (
            ("system.name", game_system), ("faction.name", faction),
            ("unit.name", unit), ("model.assembly_status", assembly_status),
            ("model.paint_status", paint_status),
        ):
            if value:
                clauses.append(f"{column} = ? COLLATE NOCASE")
                parameters.append(value)
        if is_magnetized is not None:
            clauses.append("model.is_magnetized = ?")
            parameters.append(int(is_magnetized))
        if search and search.strip():
            needle = f"%{search.strip()}%"
            clauses.append(
                "(system.name LIKE ? OR faction.name LIKE ? OR unit.name LIKE ? "
                "OR model.display_name LIKE ? OR model.storage_location LIKE ? OR model.notes LIKE ? "
                "OR model_active.name LIKE ? OR represented.name LIKE ? OR model_active.rule_model_name LIKE ? "
                "OR weapon_active.name LIKE ? OR weapon_active.loadout_name LIKE ?)"
            )
            parameters.extend([needle] * 11)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT model.id, model.unit_id, unit.reference_code AS unit_code,
                          model.reference_code AS model_code, system.name AS game_system,
                          faction.name AS faction, unit.name AS unit, unit.points AS unit_points,
                          unit.points_manual AS unit_points_manual,
                          model.display_name, model.assembly_status,
                          model.paint_status, model.is_magnetized,
                          model.storage_location, model.notes,
                          model_active.name AS current_model_configuration,
                          weapon_active.name AS current_weapon_configuration,
                          COALESCE(model_active.rule_model_name, represented.name) AS represented_unit,
                          weapon_active.loadout_name AS current_loadout,
                          COALESCE(model_active.points, weapon_active.points) AS current_points
                   FROM physical_model AS model
                   JOIN unit ON unit.id = model.unit_id
                   JOIN faction ON faction.id = unit.faction_id
                   JOIN game_system AS system ON system.id = faction.game_system_id
                   LEFT JOIN configuration AS model_active
                     ON model_active.physical_model_id = model.id
                    AND model_active.is_active = 1 AND model_active.configuration_type = 'model'
                   LEFT JOIN configuration AS weapon_active
                     ON weapon_active.physical_model_id = model.id
                    AND weapon_active.is_active = 1 AND weapon_active.configuration_type = 'weapon'
                   LEFT JOIN unit AS represented ON represented.id = model_active.represented_unit_id""" + where + """
                   ORDER BY system.name COLLATE NOCASE, faction.name COLLATE NOCASE,
                            unit.name COLLATE NOCASE, model.display_name COLLATE NOCASE,
                            model.created_at""",
                parameters,
            ).fetchall()
        return [
            CollectionRow(**dict(row) | {
                "is_magnetized": bool(row["is_magnetized"]),
                "unit_points_manual": bool(row["unit_points_manual"]),
            })
            for row in rows
        ]

    def export_faction_database(
        self, game_system: str, faction: str, destination: str | Path
    ) -> tuple[int, int]:
        """Write a self-contained SQLite copy containing only one faction."""
        target = Path(destination)
        if target.resolve() == self.database.path.resolve():
            raise CollectionError("Export destination cannot be the active WMS database")
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.database.connect() as source:
            found = source.execute(
                """SELECT faction.id FROM faction
                     JOIN game_system ON game_system.id = faction.game_system_id
                    WHERE game_system.name = ? COLLATE NOCASE
                      AND faction.name = ? COLLATE NOCASE""",
                (game_system, faction),
            ).fetchone()
            if not found:
                raise CollectionError("The selected Faction does not exist")
            handle, temporary_name = tempfile.mkstemp(
                prefix="wms-faction-", suffix=".sqlite3", dir=target.parent
            )
            os.close(handle)
            temporary = Path(temporary_name)
            try:
                with sqlite3.connect(temporary) as exported:
                    source.backup(exported)
                    exported.execute("PRAGMA journal_mode = DELETE")
                    exported.execute("PRAGMA foreign_keys = ON")
                    exported.execute(
                        """DELETE FROM faction
                            WHERE id NOT IN (
                                SELECT faction.id FROM faction
                                JOIN game_system ON game_system.id = faction.game_system_id
                                WHERE game_system.name = ? COLLATE NOCASE
                                  AND faction.name = ? COLLATE NOCASE
                            )""",
                        (game_system, faction),
                    )
                    exported.execute("DELETE FROM game_system WHERE id NOT IN (SELECT game_system_id FROM faction)")
                    counts = exported.execute(
                        """SELECT COUNT(DISTINCT unit.id), COUNT(physical_model.id)
                             FROM unit LEFT JOIN physical_model ON physical_model.unit_id = unit.id"""
                    ).fetchone()
                    exported.commit()
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        return int(counts[0]), int(counts[1])

    def import_army(self, game_system: str, faction: str, units: tuple[ImportedUnit, ...],
                    game_system_id: str = "") -> int:
        system_name = _clean_name(game_system, "game system")
        faction_name = _clean_name(faction, "faction")
        if not units:
            raise CollectionError("The army contains no units")
        timestamp = _now()

        def named_id(connection, table, name, parent_column=None, parent_id=None):
            query = f"SELECT id FROM {table} WHERE name = ? COLLATE NOCASE"
            params = [name]
            if parent_column:
                query += f" AND {parent_column} = ?"
                params.append(parent_id)
            found = connection.execute(query, params).fetchone()
            if found:
                return found["id"]
            entity_id = str(uuid4())
            columns, values = ["id", "name", "created_at", "updated_at"], [entity_id, name, timestamp, timestamp]
            if parent_column:
                columns.insert(1, parent_column); values.insert(1, parent_id)
            connection.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in values)})",
                values,
            )
            return entity_id

        added = 0
        with self.database.transaction() as connection:
            if game_system_id:
                selected = connection.execute(
                    "SELECT id, name FROM game_system WHERE id = ?", (game_system_id,)
                ).fetchone()
                if not selected or selected["name"].casefold() != system_name.casefold():
                    raise CollectionError("The selected Game System ID does not match the import target")
                system_id = selected["id"]
            else:
                system_id = named_id(connection, "game_system", system_name)
            faction_id = named_id(connection, "faction", faction_name, "game_system_id", system_id)
            for imported in units:
                unit_name = _clean_name(imported.name, "unit")
                if imported.model_count < 1:
                    raise CollectionError(f"Invalid model count for {unit_name}")
                expanded_models = [
                    (model.name, model.weapons)
                    for model in imported.physical_models
                    for _ in range(model.quantity)
                ]
                # Each imported army-list entry is a distinct owned unit, even
                # when another entry in the same faction has the same name.
                unit_id = str(uuid4())
                connection.execute(
                    """INSERT INTO unit
                       (id, faction_id, name, created_at, updated_at, reference_code, points)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (unit_id, faction_id, unit_name, timestamp, timestamp,
                     _reference_code("U", system_name, unit_name, unit_id), imported.points),
                )
                for number in range(1, imported.model_count + 1):
                    if number <= len(expanded_models):
                        base_name, weapons = expanded_models[number - 1]
                        duplicate_total = sum(1 for name, _ in expanded_models if name == base_name)
                        duplicate_number = sum(
                            1 for name, _ in expanded_models[:number] if name == base_name
                        )
                        display_name = base_name if duplicate_total == 1 else f"{base_name} {duplicate_number}"
                    else:
                        weapons = ()
                        display_name = unit_name if imported.model_count == 1 else f"{unit_name} {number}"
                    model_id = str(uuid4())
                    connection.execute(
                        """INSERT INTO physical_model
                           (id, unit_id, display_name, assembly_status, paint_status,
                            is_magnetized, storage_location, notes, created_at, updated_at,
                            reference_code)
                           VALUES (?, ?, ?, 'unassembled', 'unpainted', 0, NULL, ?, ?, ?, ?)""",
                        (model_id, unit_id, display_name, "Imported from GW App", timestamp, timestamp,
                         _reference_code("M", system_name, unit_name, model_id)),
                    )
                    added += 1
                    if weapons or imported.points is not None:
                        loadout = " + ".join(weapons) if weapons else "Default"
                        connection.execute(
                            """INSERT INTO configuration
                               (id, physical_model_id, name, is_active, notes, created_at, updated_at,
                                configuration_type, loadout_name, points)
                               VALUES (?, ?, ?, ?, ?, ?, ?, 'weapon', ?, ?)""",
                            (str(uuid4()), model_id, loadout, 1,
                             ("Weapons imported from GW App model block: " + ", ".join(weapons)
                              if weapons else "Unit points stored on the Unit instance"),
                             timestamp, timestamp, loadout, None),
                        )
        return added

    def update_model(
        self,
        model_id: str,
        *,
        display_name: str | None,
        assembly_status: str,
        paint_status: str,
        is_magnetized: bool,
        storage_location: str | None,
        notes: str | None,
    ) -> None:
        self._validate_statuses(assembly_status, paint_status)
        clean_optional = lambda value: value.strip() or None if value else None
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """UPDATE physical_model
                   SET display_name = ?, assembly_status = ?, paint_status = ?,
                       is_magnetized = ?, storage_location = ?, notes = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    clean_optional(display_name), assembly_status, paint_status,
                    int(is_magnetized), clean_optional(storage_location),
                    clean_optional(notes), _now(), model_id,
                ),
            )
            if cursor.rowcount != 1:
                raise CollectionError("Physical model does not exist")

    def bulk_update_models(
        self,
        model_ids: list[str],
        *,
        assembly_status: str | None = None,
        paint_status: str | None = None,
        is_magnetized: bool | None = None,
        location_start: tuple[int, int, int] | None = None,
        storage_location: str | None = None,
    ) -> int:
        """Update selected models atomically and optionally assign ordered locations.

        ``location_start`` contains ``(cabinet, slot, slots_per_cabinet)``.
        Model IDs are assigned locations in the order supplied by the caller.
        """
        unique_ids = list(dict.fromkeys(model_ids))
        if not unique_ids:
            raise CollectionError("Select at least one physical model")
        if assembly_status is not None and assembly_status not in ASSEMBLY_STATUSES:
            raise CollectionError(f"Unknown assembly status: {assembly_status}")
        if paint_status is not None and paint_status not in PAINT_STATUSES:
            raise CollectionError(f"Unknown paint status: {paint_status}")
        if location_start is not None and storage_location is not None:
            raise CollectionError("Choose either a shared location or a Cabinet / Slot sequence")
        if all(value is None for value in (
            assembly_status, paint_status, is_magnetized, location_start, storage_location
        )):
            raise CollectionError("Choose at least one field to update")
        if location_start is not None:
            cabinet, slot, slots_per_cabinet = location_start
            if cabinet < 1 or slot < 1 or slots_per_cabinet < 1 or slot > slots_per_cabinet:
                raise CollectionError("Cabinet and Slot values are outside the selected range")

        timestamp = _now()
        updated = 0
        with self.database.transaction() as connection:
            for offset, model_id in enumerate(unique_ids):
                assignments, values = [], []
                for column, value in (
                    ("assembly_status", assembly_status),
                    ("paint_status", paint_status),
                    ("is_magnetized", int(is_magnetized) if is_magnetized is not None else None),
                ):
                    if value is not None:
                        assignments.append(f"{column} = ?")
                        values.append(value)
                if location_start is not None:
                    cabinet, slot, slots_per_cabinet = location_start
                    absolute_slot = slot - 1 + offset
                    location = (
                        f"Cabinet {cabinet + absolute_slot // slots_per_cabinet} "
                        f"Slot {absolute_slot % slots_per_cabinet + 1}"
                    )
                    assignments.append("storage_location = ?")
                    values.append(location)
                elif storage_location is not None:
                    assignments.append("storage_location = ?")
                    values.append(storage_location.strip() or None)
                assignments.append("updated_at = ?")
                values.extend((timestamp, model_id))
                cursor = connection.execute(
                    f"UPDATE physical_model SET {', '.join(assignments)} WHERE id = ?",
                    values,
                )
                if cursor.rowcount != 1:
                    raise CollectionError("One or more selected physical models no longer exist")
                updated += 1
        return updated

    def delete_model(self, model_id: str) -> None:
        with self.database.transaction() as connection:
            cursor = connection.execute("DELETE FROM physical_model WHERE id = ?", (model_id,))
            if cursor.rowcount != 1:
                raise CollectionError("Physical model does not exist")

    def delete_models(self, model_ids: list[str]) -> int:
        """Delete exactly the selected models in one transaction."""
        unique_ids = list(dict.fromkeys(model_ids))
        if not unique_ids:
            raise CollectionError("Select at least one physical model")
        with self.database.transaction() as connection:
            for model_id in unique_ids:
                cursor = connection.execute(
                    "DELETE FROM physical_model WHERE id = ?", (model_id,)
                )
                if cursor.rowcount != 1:
                    raise CollectionError(
                        "One or more selected physical models no longer exist"
                    )
        return len(unique_ids)

    def count_models_for_game_system(self, game_system_id: str) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS model_count
                   FROM physical_model AS model
                   JOIN unit ON unit.id = model.unit_id
                   JOIN faction ON faction.id = unit.faction_id
                   WHERE faction.game_system_id = ?""",
                (game_system_id,),
            ).fetchone()
        return int(row["model_count"])

    def delete_models_for_game_system(self, game_system_id: str) -> int:
        """Delete every physical model in one game system, preserving its hierarchy."""
        with self.database.transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM game_system WHERE id = ?", (game_system_id,)
            ).fetchone()
            if not exists:
                raise CollectionError("Game system does not exist")
            cursor = connection.execute(
                """DELETE FROM physical_model
                   WHERE unit_id IN (
                       SELECT unit.id FROM unit
                       JOIN faction ON faction.id = unit.faction_id
                       WHERE faction.game_system_id = ?
                   )""",
                (game_system_id,),
            )
        return cursor.rowcount

    def update_model_status(self, model_id: str, *, assembly_status: str, paint_status: str) -> None:
        self._validate_statuses(assembly_status, paint_status)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """UPDATE physical_model
                   SET assembly_status = ?, paint_status = ?, updated_at = ?
                   WHERE id = ?""",
                (assembly_status, paint_status, _now(), model_id),
            )
            if cursor.rowcount != 1:
                raise CollectionError("Physical model does not exist")

    def delete_game_system(self, game_system_id: str) -> None:
        with self.database.transaction() as connection:
            cursor = connection.execute("DELETE FROM game_system WHERE id = ?", (game_system_id,))
            if cursor.rowcount != 1:
                raise CollectionError("Game system does not exist")

    @staticmethod
    def _validate_statuses(assembly_status: str, paint_status: str) -> None:
        if assembly_status not in ASSEMBLY_STATUSES:
            raise CollectionError(f"Unknown assembly status: {assembly_status}")
        if paint_status not in PAINT_STATUSES:
            raise CollectionError(f"Unknown paint status: {paint_status}")
