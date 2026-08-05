from __future__ import annotations

from pathlib import Path
import csv
import hashlib
import json
import re
import sqlite3
import tempfile
from dataclasses import dataclass

from .collection import ASSEMBLY_STATUSES, PAINT_STATUSES, CollectionError, CollectionService
from .gw_import import GWImportError, ImportedUnit, ParsedArmy, parse_gw_army_text
from .rules import RulesError, RulesManager


ASSEMBLY_LABELS = {
    "unassembled": "Unassembled",
    "partially_assembled": "Partially assembled",
    "assembled": "Assembled",
}
PAINT_LABELS = {
    "unpainted": "Unpainted",
    "primed": "Primed",
    "in_progress": "In progress",
    "painted": "Painted",
}


def _natural_key(value: str) -> tuple[object, ...]:
    return tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", value or "")
    )


def _profiles_for_faction(
    profiles: list[dict[str, object]], faction: str
) -> list[dict[str, object]]:
    """Limit model choices to the current faction and keep their natural order."""
    faction_key = faction.strip().casefold()
    return sorted(
        (
            profile for profile in profiles
            if str(profile.get("faction", "")).strip().casefold() == faction_key
        ),
        key=lambda profile: _natural_key(str(profile.get("name", ""))),
    )


def _collection_points(row, rules: RulesManager) -> int | None:
    """Return the persisted Unit Instance score; the database is authoritative."""
    return row.unit_points


def _standard_collection_points(row, rules: RulesManager) -> int | None:
    """Resolve the current Active Model's standard score from Unit Data JSON."""
    try:
        return rules.points(row.faction, _collection_model_name(row), row.game_system)
    except RulesError:
        return None


def _collection_points_mismatch(row, rules: RulesManager) -> bool:
    """Flag every persisted/standard difference without changing either value."""
    return row.unit_points != _standard_collection_points(row, rules)


def _display_points(value: int | None) -> str:
    """Display unresolved Points distinctly while preserving a legitimate zero."""
    return "-" if value is None else str(value)


def _resolve_unit_instance_points(parsed: ParsedArmy, rules: RulesManager,
                                  game_system: str, faction: str) -> ParsedArmy:
    """Bind confirmed Unit Data points to Unit instances, never directly from source text."""
    resolved = tuple(
        ImportedUnit(
            unit.name,
            unit.model_count,
            unit.physical_models,
            rules.points(faction, unit.name, game_system),
        )
        for unit in parsed.units
    )
    return ParsedArmy(
        parsed.faction, resolved, parsed.game_system,
        parsed.missing_profiles, parsed.detected_format,
    )


def _import_quantity_rows(units: tuple[ImportedUnit, ...], faction: str):
    """Describe the exact second-stage import quantities without merging duplicate Units."""
    rows = [
        {
            "key": index,
            "action": "Import",
            "faction": faction,
            "unit": unit.name,
            "change": f"{unit.model_count} Physical Model"
                      f"{'s' if unit.model_count != 1 else ''}",
        }
        for index, unit in enumerate(units)
    ]
    return rows, sum(unit.model_count for unit in units)


def _confirmation_includes_row(allow_selection: bool, is_checked: bool) -> bool:
    """Read-only confirmation rows are always included; selectable rows follow their checkbox."""
    return not allow_selection or is_checked


def _collection_weapon(row) -> str:
    """AoS has no selectable weapon profile; its loadout is always Default."""
    if row.game_system.strip().casefold() == "age of sigmar":
        return "Default"
    return row.current_loadout or ""


def _collection_model_name(row) -> str:
    """Return the unit name selected as active for this physical model."""
    if row.is_magnetized and row.current_model_configuration and row.represented_unit:
        return row.represented_unit
    return row.unit


@dataclass(frozen=True)
class DashboardTotal:
    game_system: str
    faction: str
    unit_instances: int
    physical_models: int
    points: int


@dataclass(frozen=True)
class ChartValue:
    label: str
    value: int


def _dashboard_chart_values(rows, totals: list[DashboardTotal], metric: str,
                            selected_system: str = "All Game Systems") -> list[ChartValue]:
    """Build every Dashboard chart from Collection database rows only."""
    filtered_rows = list(rows) if selected_system == "All Game Systems" else [
        row for row in rows if row.game_system == selected_system
    ]
    filtered_totals = totals if selected_system == "All Game Systems" else [
        item for item in totals if item.game_system == selected_system
    ]
    if metric == "system_points":
        grouped: dict[str, int] = {}
        for item in filtered_totals:
            grouped[item.game_system] = grouped.get(item.game_system, 0) + item.points
    elif metric == "system_models":
        grouped = {}
        for item in filtered_totals:
            grouped[item.game_system] = grouped.get(item.game_system, 0) + item.physical_models
    elif metric in {"faction_assembly", "faction_paint"}:
        grouped = {}
        for row in filtered_rows:
            prefix = (f"{row.game_system} · " if selected_system == "All Game Systems" else "") + row.faction
            if metric == "faction_assembly":
                state = "Assembled" if row.assembly_status == "assembled" else "Not assembled"
            else:
                state = "Painted" if row.paint_status == "painted" else "Not painted"
            label = f"{prefix} · {state}"
            grouped[label] = grouped.get(label, 0) + 1
    else:
        grouped = {}
        for item in filtered_totals:
            label = (f"{item.game_system} · " if selected_system == "All Game Systems" else "") + item.faction
            grouped[label] = grouped.get(label, 0) + item.points
    return [
        ChartValue(label, value)
        for label, value in sorted(grouped.items(), key=lambda item: (-item[1], _natural_key(item[0])))
    ]


def _points_filter_matches(row, rules: RulesManager, filter_value: str | None) -> bool:
    if filter_value == "yellow":
        return _collection_points_mismatch(row, rules)
    if filter_value == "not_yellow":
        return not _collection_points_mismatch(row, rules)
    return True


def _target_unit_size(rules: RulesManager, target_system: str, selected_faction: str,
                      detected_system: str, detected_faction: str, unit: str) -> int | None:
    """Resolve import prechecks against the selected WMS target, never detected labels."""
    del detected_system
    return rules.unit_size(detected_faction or selected_faction, unit, target_system)


def _missing_unit_points(parsed: ParsedArmy, rules: RulesManager, target_system: str,
                         target_faction: str) -> tuple[str, ...]:
    """Report missing Points directly; Unit Size is a separate validation."""
    return tuple(
        unit.name for unit in parsed.units
        if rules.points(target_faction, unit.name, target_system) is None
        and unit.points is None
    )


def _dashboard_totals(rows, rules: RulesManager) -> list[DashboardTotal]:
    """Aggregate the authoritative database score once per Unit instance."""
    units: dict[str, list] = {}
    for row in rows:
        units.setdefault(row.unit_id, []).append(row)
    grouped: dict[tuple[str, str], dict[str, int]] = {}
    for models in units.values():
        first = models[0]
        bucket = grouped.setdefault(
            (first.game_system, first.faction), {"units": 0, "models": 0, "points": 0}
        )
        bucket["units"] += 1
        bucket["models"] += len(models)
        points = _collection_points(first, rules)
        if points is not None:
            bucket["points"] += int(points)
    return [
        DashboardTotal(system, faction, values["units"], values["models"], values["points"])
        for (system, faction), values in sorted(
            grouped.items(), key=lambda item: (_natural_key(item[0][0]), _natural_key(item[0][1]))
        )
    ]


def _export_faction_csv(service: CollectionService, rules: RulesManager, game_system: str,
                        faction: str, destination: str | Path) -> tuple[int, int]:
    """Export a readable, current-state collection snapshot for one faction."""
    system_key, faction_key = game_system.strip().casefold(), faction.strip().casefold()
    rows = [
        row for row in service.list_collection()
        if row.game_system.strip().casefold() == system_key
        and row.faction.strip().casefold() == faction_key
    ]
    unit_ids = {row.unit_id for row in rows}
    with Path(destination).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow((
            "Game System", "Faction", "Unit Code", "Original Unit", "Model Code",
            "Physical Model", "Magnetized", "Active Model", "Points", "Weapon / Loadout",
            "Assembly", "Paint", "Location", "Notes",
        ))
        for row in rows:
            writer.writerow((
                row.game_system, row.faction, row.unit_code, row.unit, row.model_code,
                row.display_name or row.unit, "Yes" if row.is_magnetized else "No",
                _collection_model_name(row), _display_points(_collection_points(row, rules)),
                _collection_weapon(row), ASSEMBLY_LABELS.get(row.assembly_status, row.assembly_status),
                PAINT_LABELS.get(row.paint_status, row.paint_status), row.storage_location or "",
                row.notes or "",
            ))
    return len(unit_ids), len(rows)


def _read_wms_collection_csv(source: str | Path) -> list[dict[str, str]]:
    with Path(source).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"Game System", "Faction", "Unit Code", "Original Unit", "Physical Model"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise CollectionError("This is not a WMS Collection CSV export")
        rows = [{key: (value or "").strip() for key, value in row.items()} for row in reader]
    if not rows:
        raise CollectionError("The WMS Collection CSV contains no models")
    return rows


def _validate_wms_collection_rows(service: CollectionService, rows: list[dict[str, str]]) -> tuple[int, int]:
    """Validate an entire CSV in memory; never create database records."""
    systems = {name.casefold(): entity_id for entity_id, name in service.list_named("game_system")}
    unit_keys = set()
    for number, row in enumerate(rows, start=2):
        system_name, faction_name = row["Game System"], row["Faction"]
        unit_name, model_name = row["Original Unit"], row["Physical Model"]
        if not system_name or not faction_name or not unit_name or not model_name:
            raise CollectionError(
                f"CSV row {number} requires Game System, Faction, Original Unit, and Physical Model"
            )
        if system_name.casefold() not in systems:
            raise CollectionError(
                f"Game System '{system_name}' does not exist. Create it in Settings before importing."
            )
        points = row.get("Points", "")
        if points and (not points.isdigit() or int(points) < 0):
            raise CollectionError(f"CSV row {number} has invalid Points: {points}")
        unit_keys.add((system_name.casefold(), faction_name.casefold(), row["Unit Code"] or unit_name.casefold()))
    return len(unit_keys), len(rows)


def _import_wms_collection_csv(service: CollectionService, source: str | Path) -> tuple[int, int]:
    """Restore the current collection state from a WMS-generated CSV snapshot."""
    rows = _read_wms_collection_csv(source)
    assembly_by_label = {label.casefold(): key for key, label in ASSEMBLY_LABELS.items()}
    paint_by_label = {label.casefold(): key for key, label in PAINT_LABELS.items()}
    created_units: dict[tuple[str, str, str], str] = {}
    model_count = 0
    for row in rows:
        system_name, faction_name = row["Game System"], row["Faction"]
        unit_name = row["Original Unit"]
        if not system_name or not faction_name or not unit_name:
            raise CollectionError("Game System, Faction, and Original Unit are required")
        systems = service.list_named("game_system")
        system_id = next((entity_id for entity_id, name in systems if name.casefold() == system_name.casefold()), None)
        if system_id is None:
            raise CollectionError(
                f"Game System '{system_name}' does not exist. Create it in Settings before importing."
            )
        factions = service.list_named("faction", "game_system_id", system_id)
        faction_id = next((entity_id for entity_id, name in factions if name.casefold() == faction_name.casefold()), None)
        if faction_id is None:
            faction_id = service.create_faction(system_id, faction_name)
        unit_key = (system_name.casefold(), faction_name.casefold(), row["Unit Code"] or unit_name.casefold())
        unit_id = created_units.get(unit_key)
        if unit_id is None:
            unit_id = service.create_unit(faction_id, unit_name)
            created_units[unit_key] = unit_id
            points_text = row.get("Points", "")
            service.set_unit_points(
                unit_id, int(points_text) if points_text.isdigit() else None, manual=True
            )
        assembly = assembly_by_label.get(row.get("Assembly", "").casefold(), "unassembled")
        paint = paint_by_label.get(row.get("Paint", "").casefold(), "unpainted")
        magnetized = row.get("Magnetized", "").casefold() in {"yes", "true", "1"}
        model_id = service.create_model(
            unit_id, display_name=row["Physical Model"] or None,
            assembly_status=assembly, paint_status=paint, is_magnetized=magnetized,
            storage_location=row.get("Location") or None, notes=row.get("Notes") or None,
        )
        active_model = row.get("Active Model", "")
        if magnetized and active_model and active_model.casefold() != unit_name.casefold():
            points_text = row.get("Points", "")
            points = int(points_text) if points_text.isdigit() else None
            service.create_configuration(
                model_id, active_model, configuration_type="model", rule_faction=faction_name,
                rule_model_name=active_model, points=points, is_active=True,
            )
        loadout = row.get("Weapon / Loadout", "")
        if magnetized and loadout and loadout.casefold() != "default" and system_name.casefold() != "age of sigmar":
            service.create_configuration(
                model_id, loadout, configuration_type="weapon", loadout_name=loadout, is_active=True,
            )
        model_count += 1
    return len(created_units), model_count


def _collection_filter_values(service: CollectionService, selected_system=None,
                              selected_faction=None):
    """Return database-backed Collection filters, including empty systems/factions."""
    systems = service.list_named("game_system")
    system_id = next(
        (entity_id for entity_id, name in systems if name == selected_system), None
    )
    factions = (
        service.list_named("faction", "game_system_id", system_id)
        if system_id else [
            faction
            for current_system_id, _ in systems
            for faction in service.list_named("faction", "game_system_id", current_system_id)
        ]
    )
    faction_names = {name for _, name in factions}
    if selected_faction not in faction_names:
        selected_faction = None
    rows = service.list_collection()
    units = {
        row.unit for row in rows
        if (not selected_system or row.game_system == selected_system)
        and (not selected_faction or row.faction == selected_faction)
    }
    return {name for _, name in systems}, faction_names, units


def run_gui(service: CollectionService, rules: RulesManager) -> int:
    try:
        from PySide6.QtCore import QSettings, Qt, QRectF
        from PySide6.QtGui import QColor, QPainter, QPen
        from PySide6.QtWidgets import (
            QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
            QCompleter, QFormLayout, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
            QMainWindow, QMessageBox, QPushButton, QRadioButton, QStackedWidget, QTableWidget,
            QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget, QSpinBox, QSplitter, QFileDialog,
            QTreeWidget, QTreeWidgetItem, QMenu, QTabWidget,
            QAbstractItemView, QFrame, QGroupBox, QHeaderView, QSizePolicy,
        )
    except ImportError as exc:
        raise RuntimeError(
            'PySide6 is not installed. Run: python -m pip install -e ".[gui]"'
        ) from exc

    for game_system_id, game_system_name in service.list_named("game_system"):
        rules.bind_game_system(game_system_id, game_system_name)

    class UnitListConfirmationDialog(QDialog):
        """Scrollable confirmation surface for operations that contain Unit data."""

        def __init__(self, parent, title: str, summary: str, rows,
                     confirm_label: str = "Confirm Selected", allow_selection: bool = True,
                     secondary_label: str | None = None):
            super().__init__(parent)
            self.allow_selection = allow_selection
            self.setWindowTitle(title)
            self.resize(860, 560)
            layout = QVBoxLayout(self)
            heading = QLabel(summary)
            heading.setWordWrap(True)
            layout.addWidget(heading)
            self.units = QTreeWidget()
            self.units.setHeaderLabels(("Action", "Faction", "Unit", "Change"))
            self.units.setRootIsDecorated(False)
            self.units.setAlternatingRowColors(True)
            self.units.setSelectionMode(QAbstractItemView.ExtendedSelection)
            for row in rows:
                item = QTreeWidgetItem((
                    str(row.get("action", "")), str(row.get("faction", "")),
                    str(row.get("unit", "")), str(row.get("change", "")),
                ))
                item.setData(0, Qt.UserRole, row.get("key", row.get("unit", "")))
                if allow_selection:
                    item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                    item.setCheckState(0, Qt.Checked)
                else:
                    # QTreeWidgetItem may include ItemIsUserCheckable in its
                    # default flags.  Read-only confirmation rows are all in
                    # scope and must not be interpreted as unchecked Units.
                    item.setFlags(item.flags() & ~Qt.ItemIsUserCheckable)
                self.units.addTopLevelItem(item)
            for column in range(4):
                self.units.resizeColumnToContents(column)
            self.units.header().setStretchLastSection(True)
            layout.addWidget(self.units, 1)
            controls = QHBoxLayout()
            if allow_selection:
                select_all = QPushButton("Select All")
                select_none = QPushButton("Deselect All")
                select_all.clicked.connect(lambda: self._set_all(Qt.Checked))
                select_none.clicked.connect(lambda: self._set_all(Qt.Unchecked))
                controls.addWidget(select_all)
                controls.addWidget(select_none)
            controls.addStretch()
            self.selected_action = ""
            confirm = QPushButton(confirm_label)
            cancel = QPushButton("Cancel")
            confirm.clicked.connect(self._accept_checked)
            cancel.clicked.connect(self.reject)
            controls.addWidget(confirm)
            if secondary_label:
                secondary = QPushButton(secondary_label)
                secondary.clicked.connect(self._accept_secondary)
                controls.addWidget(secondary)
            controls.addWidget(cancel)
            layout.addLayout(controls)

        def _set_all(self, state):
            for index in range(self.units.topLevelItemCount()):
                self.units.topLevelItem(index).setCheckState(0, state)

        def _accept_checked(self):
            if not self.selected_keys():
                QMessageBox.information(self, "Nothing selected", "Select at least one Unit to continue.")
                return
            self.selected_action = "primary"
            self.accept()

        def _accept_secondary(self):
            self.selected_action = "secondary"
            self.accept()

        def selected_keys(self) -> set[object]:
            selected = set()
            for index in range(self.units.topLevelItemCount()):
                item = self.units.topLevelItem(index)
                if _confirmation_includes_row(
                    self.allow_selection, item.checkState(0) == Qt.Checked
                ):
                    selected.add(item.data(0, Qt.UserRole))
            return selected

    def choose_pdf_source(parent, title: str, saved_url: str = ""):
        """Ask for a local PDF or PDF URL and return (kind, value)."""
        source_type, ok = QInputDialog.getItem(
            parent, title, "PDF source", ["Local PDF file", "PDF URL"], 0, False
        )
        if not ok:
            return None
        if source_type == "Local PDF file":
            filename, _ = QFileDialog.getOpenFileName(
                parent, title, "", "PDF files (*.pdf)"
            )
            return ("file", Path(filename)) if filename else None
        url, ok = QInputDialog.getText(
            parent, title, "PDF URL", QLineEdit.Normal, saved_url
        )
        return ("url", url.strip()) if ok and url.strip() else None

    class ModelDialog(QDialog):
        def __init__(self, parent=None, row=None):
            super().__init__(parent)
            self.setWindowTitle("Edit physical model" if row else "Add physical model")
            form = QFormLayout(self)
            self.name = QLineEdit(row.display_name or "" if row else "")
            self.assembly = QComboBox()
            self.paint = QComboBox()
            for value in sorted(ASSEMBLY_STATUSES):
                self.assembly.addItem(ASSEMBLY_LABELS[value], value)
            for value in sorted(PAINT_STATUSES):
                self.paint.addItem(PAINT_LABELS[value], value)
            self.magnetized = QCheckBox("Yes")
            self.alternative_model = QComboBox()
            self.current_weapon = QComboBox()
            self.current_weapon.addItem("No current weapon configuration", None)
            self.alternative_model.setEditable(True)
            self.alternative_model.setInsertPolicy(QComboBox.NoInsert)
            self.alternative_model.setMaxVisibleItems(20)
            self.alternative_model.setMinimumContentsLength(34)
            self.current_weapon.setEditable(True)
            self.current_weapon.setInsertPolicy(QComboBox.NoInsert)
            self.alternative_model.lineEdit().setPlaceholderText("Search or enter an alternative model")
            self.current_weapon.lineEdit().setPlaceholderText("Choose a saved weapon or enter a new loadout")
            self.active_original = QRadioButton("Original Unit")
            self.active_alternative = QRadioButton("Alternative Model")
            self.active_model_group = QButtonGroup(self)
            self.active_model_group.addButton(self.active_original)
            self.active_model_group.addButton(self.active_alternative)
            self.active_original.setChecked(True)
            self.points = QSpinBox()
            self.points.setRange(0, 999999)
            self.reset_points_requested = False
            self.location = QLineEdit()
            self.notes = QTextEdit()
            if row:
                self.assembly.setCurrentIndex(self.assembly.findData(row.assembly_status))
                self.paint.setCurrentIndex(self.paint.findData(row.paint_status))
                self.magnetized.setChecked(row.is_magnetized)
                self.location.setText(row.storage_location or "")
                self.notes.setPlainText(row.notes or "")
                configurations = service.list_configurations(row.id)
                active_model = next((item for item in configurations
                                     if item.configuration_type == "model" and item.is_active), None)
                active_weapon = next((item for item in configurations
                                      if item.configuration_type == "weapon" and item.is_active), None)
                is_aos = row.game_system.strip().casefold() == "age of sigmar"
                if is_aos:
                    self.current_weapon.clear()
                    self.current_weapon.addItem("Default", ("default_weapon",))
                    self.current_weapon.setEditable(False)
                existing_model_keys = {
                    ((item.rule_faction or "").casefold(), (item.rule_model_name or "").casefold())
                    for item in configurations if item.configuration_type == "model"
                }
                try:
                    profiles = rules.list_profiles(row.game_system)
                except RulesError:
                    profiles = []
                base_points = rules.points(row.faction, row.unit, row.game_system)
                for profile in _profiles_for_faction(profiles, row.faction):
                    faction = str(profile.get("faction", "")).strip()
                    model_name = str(profile.get("name", "")).strip()
                    if not model_name or (faction.casefold(), model_name.casefold()) in existing_model_keys:
                        continue
                    profile_points = profile.get("points")
                    unit_size = profile.get("unit_size")
                    details = []
                    if unit_size not in (None, ""):
                        details.append(f"{unit_size} models")
                    if profile_points not in (None, ""):
                        details.append(f"{profile_points} pts")
                    label = model_name
                    if details:
                        label += f"  —  {' · '.join(details)}"
                    self.alternative_model.addItem(
                        label, ("profile", faction, model_name, profile_points, unit_size)
                    )
                for configuration in configurations:
                    if is_aos and configuration.configuration_type == "weapon":
                        continue
                    target = configuration.rule_model_name or configuration.represented_unit
                    label = f"{configuration.name} — {target or configuration.loadout_name}"
                    combo = self.alternative_model if configuration.configuration_type == "model" else self.current_weapon
                    combo.addItem(label, ("configuration", configuration.id))
                    if configuration.is_active:
                        combo.setCurrentIndex(combo.count() - 1)
                        if configuration.configuration_type == "model":
                            self.active_alternative.setChecked(True)
                model_completer = QCompleter(self.alternative_model.model(), self.alternative_model)
                model_completer.setCaseSensitivity(Qt.CaseInsensitive)
                model_completer.setFilterMode(Qt.MatchContains)
                model_completer.setCompletionMode(QCompleter.PopupCompletion)
                model_completer.setMaxVisibleItems(20)
                self.alternative_model.setCompleter(model_completer)
                active = active_model or active_weapon
                if base_points is not None:
                    self.points.setValue(base_points)
                if row.is_magnetized and active and active.points is not None:
                    self.points.setValue(active.points)
            form.addRow("Display name", self.name)
            form.addRow("Assembly", self.assembly)
            form.addRow("Paint", self.paint)
            form.addRow("Magnetized", self.magnetized)
            if row:
                form.addRow("Alternative Model", self.alternative_model)
                active_choices = QHBoxLayout()
                self.active_original.setText(row.unit)
                active_choices.addWidget(self.active_original)
                active_choices.addWidget(self.active_alternative)
                form.addRow("Active", active_choices)
                form.addRow("Current weapon", self.current_weapon)
                points_row = QHBoxLayout()
                points_row.addWidget(self.points, 1)
                self.reset_points = QPushButton("Reset to Unit Data")
                points_row.addWidget(self.reset_points)
                form.addRow("Points", points_row)
            form.addRow("Storage location", self.location)
            form.addRow("Notes", self.notes)
            buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            form.addRow(buttons)

            def model_changed():
                selected_text = self.alternative_model.currentText().split("  —  ", 1)[0].strip()
                self.active_alternative.setText(
                    f"Alternative: {selected_text}" if selected_text else "Alternative Model"
                )
                if not self.active_alternative.isChecked():
                    return
                data = self.alternative_model.currentData()
                if isinstance(data, tuple) and data and data[0] == "profile":
                    profile_points = rules.points(data[1], data[2], row.game_system)
                    if profile_points is not None:
                        self.points.setValue(int(profile_points))
                elif isinstance(data, tuple) and data and data[0] == "configuration":
                    selected = next((item for item in configurations if item.id == data[1]), None)
                    if selected and selected.points is not None:
                        self.points.setValue(int(selected.points))

            def active_changed():
                if self.active_original.isChecked():
                    original_points = rules.points(row.faction, row.unit, row.game_system) if row else None
                    if original_points is not None:
                        self.points.setValue(int(original_points))
                else:
                    model_changed()

            def update_configuration_controls():
                enabled = self.magnetized.isChecked()
                self.alternative_model.setEnabled(enabled)
                self.active_original.setEnabled(enabled)
                self.active_alternative.setEnabled(enabled)
                self.current_weapon.setEnabled(enabled and not (
                    row and row.game_system.strip().casefold() == "age of sigmar"
                ))
                self.points.setEnabled(enabled)
                if hasattr(self, "reset_points"):
                    self.reset_points.setEnabled(enabled)

            def reset_to_unit_data():
                active_name, active_faction = row.unit, row.faction
                if self.magnetized.isChecked() and self.active_alternative.isChecked():
                    data = self.alternative_model.currentData()
                    if isinstance(data, tuple) and data and data[0] == "profile":
                        active_faction, active_name = data[1], data[2]
                    elif isinstance(data, tuple) and data and data[0] == "configuration":
                        selected = next((item for item in configurations if item.id == data[1]), None)
                        if selected:
                            active_faction = selected.rule_faction or row.faction
                            active_name = selected.rule_model_name or selected.represented_unit or row.unit
                    else:
                        active_name = self.alternative_model.currentText().split("  —  ", 1)[0].strip()
                standard = rules.points(active_faction, active_name, row.game_system) if active_name else None
                if standard is None:
                    QMessageBox.information(
                        self, "Points unavailable",
                        "The current Active Model has no Points in Unit Data JSON."
                    )
                    return
                self.points.setValue(int(standard))
                self.reset_points_requested = True

            self.alternative_model.currentIndexChanged.connect(model_changed)
            self.active_original.toggled.connect(active_changed)
            self.magnetized.toggled.connect(update_configuration_controls)
            if hasattr(self, "reset_points"):
                self.reset_points.clicked.connect(reset_to_unit_data)
            model_changed()
            if row and row.unit_points is not None:
                self.points.setValue(int(row.unit_points))
            update_configuration_controls()

        def values(self):
            return dict(
                display_name=self.name.text(),
                assembly_status=self.assembly.currentData(),
                paint_status=self.paint.currentData(),
                is_magnetized=self.magnetized.isChecked(),
                storage_location=self.location.text(),
                notes=self.notes.toPlainText(),
            )

    class CollectionPage(QWidget):
        HEADERS = (
            "Name", "Models", "Game System", "Faction", "Physical Model", "Assembly",
            "Paint", "Magnetized", "Active Model", "Current Weapon State", "Model", "Weapon / Loadout",
            "Points", "Location", "Notes", "Unit Code", "Model Code",
        )

        def __init__(self):
            super().__init__()
            self.rows = []
            layout = QVBoxLayout(self)
            filters = QGridLayout()
            self.system_filter, self.faction_filter, self.unit_filter = QComboBox(), QComboBox(), QComboBox()
            self.assembly_filter, self.paint_filter, self.magnetized_filter = QComboBox(), QComboBox(), QComboBox()
            self.points_filter = QComboBox()
            self.search_filter = QLineEdit()
            self.search_filter.setPlaceholderText("Name, location, or notes")
            for combo, label in (
                (self.system_filter, "All systems"), (self.faction_filter, "All factions"),
                (self.unit_filter, "All units"), (self.assembly_filter, "All assembly states"),
                (self.paint_filter, "All paint states"), (self.magnetized_filter, "Any magnetization"),
            ):
                combo.addItem(label, None)
            for value in sorted(ASSEMBLY_STATUSES): self.assembly_filter.addItem(ASSEMBLY_LABELS[value], value)
            for value in sorted(PAINT_STATUSES): self.paint_filter.addItem(PAINT_LABELS[value], value)
            self.magnetized_filter.addItem("Magnetized", True)
            self.magnetized_filter.addItem("Not magnetized", False)
            self.points_filter.addItem("All point values", None)
            self.points_filter.addItem("Yellow highlighted", "yellow")
            self.points_filter.addItem("Not yellow highlighted", "not_yellow")
            for column, (label, widget) in enumerate((
                ("System", self.system_filter), ("Faction", self.faction_filter),
                ("Unit", self.unit_filter), ("Assembly", self.assembly_filter),
                ("Paint", self.paint_filter), ("Magnetized", self.magnetized_filter),
                ("Points", self.points_filter), ("Search", self.search_filter),
            )):
                filters.addWidget(QLabel(label), 0, column)
                filters.addWidget(widget, 1, column)
            layout.addLayout(filters)
            actions = QHBoxLayout()
            for label, callback in (
                ("Add model", self.add_model), ("Edit", self.edit_model),
                ("Copy", self.copy_selection), ("Delete", self.delete_model), ("Refresh", self.refresh),
            ):
                button = QPushButton(label)
                button.clicked.connect(callback)
                actions.addWidget(button)
            actions.addStretch()
            self.table = QTreeWidget()
            self.table.setColumnCount(len(self.HEADERS))
            self.table.setHeaderLabels(self.HEADERS)
            self.table.setSelectionBehavior(QTreeWidget.SelectRows)
            self.table.setSelectionMode(QTreeWidget.ExtendedSelection)
            self.table.setEditTriggers(QTreeWidget.NoEditTriggers)
            self.table.itemDoubleClicked.connect(self.edit_model)
            self.table.setContextMenuPolicy(Qt.CustomContextMenu)
            self.table.customContextMenuRequested.connect(self.show_collection_menu)
            self.table.header().setContextMenuPolicy(Qt.CustomContextMenu)
            self.table.header().customContextMenuRequested.connect(self.show_column_menu)
            layout.addLayout(actions)
            layout.addWidget(self.table)
            self.system_filter.currentIndexChanged.connect(self.refresh_all)
            self.faction_filter.currentIndexChanged.connect(self.refresh_all)
            for combo in (self.unit_filter, self.assembly_filter, self.paint_filter,
                          self.magnetized_filter, self.points_filter):
                combo.currentIndexChanged.connect(self.refresh)
            self.search_filter.textChanged.connect(self.refresh)
            self.restore_visible_columns()
            self.refresh_filters()
            self.refresh()

        def restore_visible_columns(self):
            settings = QSettings("WMS", "Warhammer Management System")
            defaults = [label for label in self.HEADERS if label not in {"Unit Code", "Model Code"}]
            saved = settings.value("collection/visible_columns", defaults)
            if isinstance(saved, str):
                saved = [saved]
            visible = set(saved or self.HEADERS)
            visible.add("Name")
            for column, label in enumerate(self.HEADERS):
                self.table.setColumnHidden(column, label not in visible)

        def save_visible_columns(self):
            visible = [
                label for column, label in enumerate(self.HEADERS)
                if not self.table.isColumnHidden(column)
            ]
            QSettings("WMS", "Warhammer Management System").setValue(
                "collection/visible_columns", visible
            )

        def show_column_menu(self, position):
            menu = QMenu(self)
            for column, label in enumerate(self.HEADERS):
                action = menu.addAction(label)
                action.setCheckable(True)
                action.setChecked(not self.table.isColumnHidden(column))
                action.setEnabled(column != 0)
                action.toggled.connect(
                    lambda checked, index=column: self.set_column_visible(index, checked)
                )
            menu.exec(self.table.header().mapToGlobal(position))

        def set_column_visible(self, column, visible):
            self.table.setColumnHidden(column, not visible)
            self.save_visible_columns()

        def show_collection_menu(self, position):
            item = self.table.itemAt(position)
            if item is None:
                return
            if not item.isSelected():
                self.table.clearSelection()
                item.setSelected(True)
                self.table.setCurrentItem(item)
            kind = item.data(0, Qt.UserRole + 1)
            menu = QMenu(self)
            if kind in {"unit", "single"}:
                menu.addAction("Edit Unit" if kind == "unit" else "Edit Physical Model", self.edit_model)
                menu.addAction("Copy Unit", self.copy_selected_units)
                menu.addAction("Delete Unit", self.delete_selected_units)
            if kind in {"model", "single"}:
                if kind == "single":
                    menu.addSeparator()
                else:
                    menu.addAction("Edit Physical Model", self.edit_model)
                model_rows = self.selected_rows()
                if len(model_rows) == 1:
                    configurations = service.list_configurations(model_rows[0].id)
                    for kind, title in (("model", "Active Model"), ("weapon", "Current Weapon")):
                        configuration_menu = menu.addMenu(title)
                        typed = [c for c in configurations if c.configuration_type == kind]
                        no_configuration = configuration_menu.addAction(
                            "Original model" if kind == "model" else "No current weapon"
                        )
                        no_configuration.setCheckable(True)
                        no_configuration.setChecked(not any(c.is_active for c in typed))
                        no_configuration.triggered.connect(
                            lambda _checked=False, model_id=model_rows[0].id, config_type=kind:
                            self.clear_configuration(model_id, config_type)
                        )
                        for configuration in typed:
                            target = configuration.rule_model_name or configuration.represented_unit
                            label = f"{configuration.name} — {target or configuration.loadout_name}"
                            action = configuration_menu.addAction(label)
                            action.setCheckable(True)
                            action.setChecked(configuration.is_active)
                            action.triggered.connect(
                                lambda _checked=False, model_id=model_rows[0].id,
                                configuration_id=configuration.id:
                                self.activate_configuration(model_id, configuration_id)
                            )
                    menu.addAction(
                        "Manage Alternative Models…",
                        lambda: self.manage_configurations(model_rows[0]),
                    )
                    menu.addSeparator()
                menu.addAction("Copy Physical Model", self.copy_selected_models)
                menu.addAction("Delete Physical Model", self.delete_model)
            menu.addSeparator()
            menu.addAction("Refresh", self.refresh_all)
            menu.exec(self.table.viewport().mapToGlobal(position))

        def refresh_filters(self):
            def refill(combo, values):
                selected = combo.currentData()
                combo.blockSignals(True)
                while combo.count() > 1: combo.removeItem(1)
                for value in sorted(values, key=str.casefold): combo.addItem(value, value)
                index = combo.findData(selected)
                combo.setCurrentIndex(index if index >= 0 else 0)
                combo.blockSignals(False)

            system_values, _, _ = _collection_filter_values(service)
            refill(self.system_filter, system_values)
            selected_system = self.system_filter.currentData()
            _, faction_values, _ = _collection_filter_values(service, selected_system)
            refill(self.faction_filter, faction_values)
            selected_faction = self.faction_filter.currentData()
            _, _, unit_values = _collection_filter_values(
                service, selected_system, selected_faction
            )
            refill(self.unit_filter, unit_values)

        def refresh_all(self):
            self.refresh_filters()
            self.refresh()

        def refresh(self):
            self.rows = service.list_collection(
                game_system=self.system_filter.currentData(), faction=self.faction_filter.currentData(),
                unit=self.unit_filter.currentData(), assembly_status=self.assembly_filter.currentData(),
                paint_status=self.paint_filter.currentData(), is_magnetized=self.magnetized_filter.currentData(),
                search=self.search_filter.text(),
            )
            self.rows = [
                row for row in self.rows
                if _points_filter_matches(row, rules, self.points_filter.currentData())
            ]
            self.table.clear()
            groups = {}
            for row in self.rows:
                groups.setdefault((row.unit_id, row.game_system, row.faction, row.unit), []).append(row)
            for (_unit_id, system, faction, unit), models in sorted(
                groups.items(), key=lambda item: (
                    _natural_key(item[0][3]), _natural_key(item[0][1]),
                    _natural_key(item[0][2]), item[0][0]
                )
            ):
                models.sort(key=lambda model: _natural_key(model.display_name or model.unit))
                assembly = self._summary(models, "assembly_status", ASSEMBLY_LABELS)
                paint = self._summary(models, "paint_status", PAINT_LABELS)
                magnetized = self._plain_summary(
                    ["Yes" if model.is_magnetized else "No" for model in models]
                )
                locations = self._plain_summary([model.storage_location or "" for model in models])
                if len(models) == 1:
                    model = models[0]
                    display_points = _collection_points(model, rules)
                    values = (
                        _collection_model_name(model), "1", system, faction,
                        model.display_name or "", assembly, paint,
                        magnetized, _collection_model_name(model),
                        model.current_weapon_configuration or "",
                        _collection_model_name(model), _collection_weapon(model),
                        _display_points(display_points),
                        locations, model.notes or "", model.unit_code, model.model_code,
                    )
                    item = QTreeWidgetItem(values)
                    item.setData(0, Qt.UserRole, [model])
                    item.setData(0, Qt.UserRole + 1, "single")
                    if _collection_points_mismatch(model, rules):
                        item.setBackground(12, QColor("#d9a900"))
                        item.setForeground(12, QColor("#111111"))
                        item.setToolTip(12, "Database Points differ from current Unit Data JSON")
                    self.table.addTopLevelItem(item)
                    continue
                parent = QTreeWidgetItem((
                    self._plain_summary([_collection_model_name(model) for model in models]),
                    str(len(models)), system, faction, "", assembly, paint, magnetized,
                    self._plain_summary([_collection_model_name(model) for model in models]),
                    self._plain_summary([model.current_weapon_configuration or "" for model in models]),
                    self._plain_summary([_collection_model_name(model) for model in models]),
                    self._plain_summary([_collection_weapon(model) for model in models]),
                    _display_points(_collection_points(models[0], rules)),
                    locations, self._plain_summary([model.notes or "" for model in models]),
                    models[0].unit_code, "",
                ))
                parent.setData(0, Qt.UserRole, models)
                parent.setData(0, Qt.UserRole + 1, "unit")
                if _collection_points_mismatch(models[0], rules):
                    parent.setBackground(12, QColor("#d9a900"))
                    parent.setForeground(12, QColor("#111111"))
                    parent.setToolTip(12, "Database Points differ from current Unit Data JSON")
                self.table.addTopLevelItem(parent)
                for number, model in enumerate(models, 1):
                    child = QTreeWidgetItem((
                        model.display_name or f"Model {number}", "1", system, faction,
                        model.display_name or "", ASSEMBLY_LABELS[model.assembly_status],
                        PAINT_LABELS[model.paint_status],
                        "Yes" if model.is_magnetized else "No", _collection_model_name(model),
                        model.current_weapon_configuration or "",
                        _collection_model_name(model), _collection_weapon(model),
                        "",  # Points belong to the Unit instance, never a Physical Model.
                        model.storage_location or "",
                        model.notes or "", model.unit_code, model.model_code,
                    ))
                    child.setData(0, Qt.UserRole, [model])
                    child.setData(0, Qt.UserRole + 1, "model")
                    parent.addChild(child)
            for column in range(len(self.HEADERS)):
                self.table.resizeColumnToContents(column)

        def activate_configuration(self, model_id, configuration_id):
            try:
                service.set_active_configuration(model_id, configuration_id)
            except CollectionError as exc:
                QMessageBox.warning(self, "Could not change configuration", str(exc))
                return
            self.refresh()

        def clear_configuration(self, model_id, configuration_type):
            try:
                service.clear_active_configuration(model_id, configuration_type)
            except CollectionError as exc:
                QMessageBox.warning(self, "Could not change configuration", str(exc))
                return
            self.refresh()

        def manage_configurations(self, row):
            dialog = QDialog(self)
            dialog.setWindowTitle(f"Alternative Models — {row.display_name or row.unit}")
            layout = QVBoxLayout(dialog)
            layout.addWidget(QLabel(
                "Add the models this Physical Model can represent, then explicitly choose "
                "which one is Active. Points follow the Active Model."
            ))
            table = QTableWidget(0, 6)
            table.setHorizontalHeaderLabels(("Active", "State", "Type", "Alternative Model", "Weapon / Loadout", "Points"))
            table.setSelectionBehavior(QTableWidget.SelectRows)
            table.setEditTriggers(QTableWidget.NoEditTriggers)
            layout.addWidget(table)

            form = QFormLayout()
            state_name, configuration_type, rule_model, loadout = QLineEdit(), QComboBox(), QComboBox(), QLineEdit()
            points = QSpinBox()
            points.setRange(0, 999999)
            configuration_type.addItem("Model", "model")
            configuration_type.addItem("Weapon", "weapon")
            system = service.game_system_for_model(row.id)
            try:
                profiles = rules.list_profiles(system)
            except RulesError as exc:
                profiles = []
                QMessageBox.warning(dialog, "Could not read model rules", str(exc))
            for profile in _profiles_for_faction(profiles, row.faction):
                faction = str(profile.get("faction", "")).strip()
                model_name = str(profile.get("name", "")).strip()
                profile_points = profile.get("points")
                label = f"{faction} — {model_name}" if faction else model_name
                rule_model.addItem(label, (faction, model_name, profile_points))
            rule_model.setEditable(True)
            rule_model.setInsertPolicy(QComboBox.NoInsert)
            rule_model.setCurrentIndex(-1)
            form.addRow("State name", state_name)
            form.addRow("Configuration type", configuration_type)
            form.addRow("Model from JSON rule", rule_model)
            form.addRow("Weapon / Loadout", loadout)
            form.addRow("Points", points)
            layout.addLayout(form)
            actions = QHBoxLayout()
            add_button = QPushButton("Add alternative")
            activate_button = QPushButton("Make selected Active Model")
            delete_button = QPushButton("Delete selected state")
            actions.addWidget(add_button); actions.addWidget(activate_button); actions.addWidget(delete_button)
            layout.addLayout(actions)
            close_buttons = QDialogButtonBox(QDialogButtonBox.Close)
            close_buttons.rejected.connect(dialog.reject)
            layout.addWidget(close_buttons)

            configurations = []
            def reload_table():
                nonlocal configurations
                configurations = service.list_configurations(row.id)
                table.setRowCount(len(configurations))
                for index, configuration in enumerate(configurations):
                    values = (
                        "●" if configuration.is_active else "",
                        configuration.name,
                        configuration.configuration_type.title(),
                        configuration.rule_model_name or configuration.represented_unit or "",
                        configuration.loadout_name or "",
                        "" if configuration.points is None else str(configuration.points),
                    )
                    for column, value in enumerate(values):
                        table.setItem(index, column, QTableWidgetItem(value))
                table.resizeColumnsToContents()

            def add_configuration():
                kind = configuration_type.currentData()
                selected_profile = rule_model.currentData() if kind == "model" else None
                typed_model = rule_model.currentText().strip() if kind == "model" else ""
                if selected_profile:
                    faction, model_name, profile_points = selected_profile
                else:
                    faction, model_name, profile_points = row.faction, typed_model, None
                if kind == "model" and not model_name:
                    QMessageBox.warning(dialog, "Model required", "Choose a JSON model or enter a custom model name.")
                    return
                try:
                    service.create_configuration(
                        row.id, state_name.text().strip() or model_name or loadout.text().strip(),
                        configuration_type=kind,
                        rule_faction=faction, rule_model_name=model_name,
                        loadout_name=loadout.text() if kind == "weapon" else None,
                        points=points.value() or (int(profile_points) if profile_points is not None else 0),
                        is_active=False,
                    )
                except CollectionError as exc:
                    QMessageBox.warning(dialog, "Could not add configuration", str(exc))
                    return
                state_name.clear(); loadout.clear(); points.setValue(0)
                reload_table(); self.refresh()

            def selected_configuration():
                index = table.currentRow()
                return configurations[index] if 0 <= index < len(configurations) else None

            def activate_selected():
                configuration = selected_configuration()
                if configuration:
                    self.activate_configuration(row.id, configuration.id)
                    reload_table()

            def delete_selected():
                configuration = selected_configuration()
                if not configuration:
                    return
                try:
                    service.delete_configuration(row.id, configuration.id)
                except CollectionError as exc:
                    QMessageBox.warning(dialog, "Could not delete configuration", str(exc))
                    return
                reload_table(); self.refresh()

            add_button.clicked.connect(add_configuration)
            activate_button.clicked.connect(activate_selected)
            delete_button.clicked.connect(delete_selected)
            def update_type_fields():
                is_model = configuration_type.currentData() == "model"
                rule_model.setEnabled(is_model)
                loadout.setEnabled(not is_model)
                if is_model and rule_model.currentData():
                    profile_points = rule_model.currentData()[2]
                    if profile_points is not None:
                        points.setValue(int(profile_points))
            configuration_type.currentIndexChanged.connect(update_type_fields)
            rule_model.currentIndexChanged.connect(update_type_fields)
            update_type_fields()
            reload_table()
            dialog.exec()

        @staticmethod
        def _plain_summary(values):
            distinct = {value for value in values}
            return next(iter(distinct)) if len(distinct) == 1 else "Mixed"

        @classmethod
        def _summary(cls, models, attribute, labels):
            return cls._plain_summary([labels[getattr(model, attribute)] for model in models])

        def selected_rows(self):
            selected = {}
            for item in self.table.selectedItems():
                for row in item.data(0, Qt.UserRole) or []:
                    selected[row.id] = row
            return list(selected.values())

        def copy_selection(self):
            selected = self.selected_rows()
            if not selected:
                QMessageBox.information(self, "Select models or units", "Select at least one Collection row first.")
                return
            mode, ok = QInputDialog.getItem(
                self, "Copy Collection entries", "Copy mode",
                ("Copy selected physical model(s)", "Copy complete unit(s) as new database entries"),
                0, False,
            )
            if not ok:
                return
            try:
                if mode.startswith("Copy selected physical"):
                    count = service.copy_models([row.id for row in selected])
                    message = f"Created {count} new physical model database entries."
                else:
                    units, models = service.copy_units([row.unit_id for row in selected])
                    message = f"Created {units} new Unit entries containing {models} copied physical models."
            except CollectionError as exc:
                QMessageBox.warning(self, "Could not copy selection", str(exc))
                return
            self.refresh_all()
            QMessageBox.information(self, "Copy complete", message)

        def copy_selected_models(self):
            selected = self.selected_rows()
            if not selected:
                return
            try:
                count = service.copy_models([row.id for row in selected])
            except CollectionError as exc:
                QMessageBox.warning(self, "Could not copy selection", str(exc))
                return
            self.refresh_all()
            QMessageBox.information(
                self, "Copy complete", f"Created {count} new physical model database entries."
            )

        def copy_selected_units(self):
            selected = self.selected_rows()
            if not selected:
                return
            try:
                units, models = service.copy_units([row.unit_id for row in selected])
            except CollectionError as exc:
                QMessageBox.warning(self, "Could not copy selection", str(exc))
                return
            self.refresh_all()
            QMessageBox.information(
                self, "Copy complete",
                f"Created {units} new Unit entries containing {models} copied physical models.",
            )

        def delete_selected_units(self):
            selected = self.selected_rows()
            unit_rows = {}
            for row in selected:
                unit_rows.setdefault(row.unit_id, []).append(row)
            if not unit_rows:
                return
            names = []
            for rows in unit_rows.values():
                row = rows[0]
                names.append(f"{row.game_system} / {row.faction} / {row.unit}")
            answer = QMessageBox.warning(
                self, "Delete complete Unit entries?",
                "Delete these Unit database entries and every Physical Model inside them?\n\n"
                + "\n".join(names), QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                return
            try:
                service.delete_units(list(unit_rows))
            except CollectionError as exc:
                QMessageBox.warning(self, "Could not delete units", str(exc))
                return
            self.refresh_all()

        def choose_or_create(self, title, table, parent_column=None, parent_id=None):
            items = service.list_named(table, parent_column, parent_id)
            labels = [name for _, name in items]
            if table != "game_system":
                labels.append("+ Create new…")
            if not labels:
                QMessageBox.information(
                    self, "No Game System",
                    "Create a Game System in Settings before adding Collection models."
                )
                return None
            chosen, ok = QInputDialog.getItem(self, title, title, labels, 0, False)
            if not ok:
                return None
            if chosen != "+ Create new…":
                return next(entity_id for entity_id, name in items if name == chosen)
            name, ok = QInputDialog.getText(self, title, "Name")
            if not ok:
                return None
            if table == "faction":
                return service.create_faction(parent_id, name)
            return service.create_unit(parent_id, name)

        def add_model(self):
            try:
                system = self.choose_or_create("Game system", "game_system")
                if not system: return
                faction = self.choose_or_create("Faction", "faction", "game_system_id", system)
                if not faction: return
                unit = self.choose_or_create("Unit", "unit", "faction_id", faction)
                if not unit: return
                dialog = ModelDialog(self)
                if dialog.exec() == QDialog.Accepted:
                    service.create_model(unit, **dialog.values())
                    self.refresh_all()
            except CollectionError as exc:
                QMessageBox.warning(self, "Could not add model", str(exc))

        def edit_model(self, *_):
            selected = self.selected_rows()
            if not selected:
                QMessageBox.information(self, "Select models", "Select one or more models first.")
                return
            current_item = self.table.currentItem()
            current_kind = current_item.data(0, Qt.UserRole + 1) if current_item else None
            if current_kind == "unit" and len({row.unit_id for row in selected}) == 1:
                self.edit_unit(selected)
                return
            if len(selected) > 1:
                self.bulk_edit(selected)
                return
            row = selected[0]
            dialog = ModelDialog(self, row)
            if dialog.exec() == QDialog.Accepted:
                if self.apply_model_dialog(row, dialog):
                    self.refresh()

        def apply_model_dialog(self, row, dialog):
            """Save one physical model and its independently selected active states."""
            try:
                service.update_model(row.id, **dialog.values())
                standard_points = rules.points(row.faction, row.unit, row.game_system)
                if not dialog.magnetized.isChecked() or dialog.active_original.isChecked():
                    service.clear_active_configuration(
                        row.id, "model", unit_points=standard_points
                    )
                else:
                    combo = dialog.alternative_model
                    data = combo.currentData()
                    exact_item = combo.currentIndex() >= 0 and combo.currentText() == combo.itemText(combo.currentIndex())
                    if exact_item and isinstance(data, tuple) and data[0] == "configuration":
                        selected = next(
                            item for item in service.list_configurations(row.id)
                            if item.id == data[1]
                        )
                        active_name = selected.rule_model_name or selected.represented_unit
                        active_faction = selected.rule_faction or row.faction
                        standard_points = rules.points(
                            active_faction, active_name, row.game_system
                        ) if active_name else None
                        service.set_active_configuration(
                            row.id, data[1], unit_points=standard_points
                        )
                    elif exact_item and isinstance(data, tuple) and data[0] == "profile":
                        _, faction, model_name, profile_points, _unit_size = data
                        standard_points = rules.points(faction, model_name, row.game_system)
                        service.create_configuration(
                            row.id, model_name, configuration_type="model",
                            rule_faction=faction, rule_model_name=model_name,
                            points=dialog.points.value() if dialog.points.value() else (
                                int(profile_points) if profile_points is not None else 0
                            ), is_active=True, unit_points=standard_points,
                        )
                    else:
                        typed = combo.currentText().strip()
                        if not typed:
                            raise CollectionError(
                                "Choose or enter an Alternative Model before making it Active"
                            )
                        standard_points = rules.points(row.faction, typed, row.game_system)
                        service.create_configuration(
                            row.id, typed, configuration_type="model",
                            rule_faction=row.faction, rule_model_name=typed,
                            points=dialog.points.value(), is_active=True,
                            unit_points=standard_points,
                        )
                for kind, combo in (("weapon", dialog.current_weapon),):
                    if kind == "weapon" and row.game_system.strip().casefold() == "age of sigmar":
                        service.clear_active_configuration(row.id, "weapon")
                        continue
                    data = combo.currentData()
                    exact_item = combo.currentIndex() >= 0 and combo.currentText() == combo.itemText(combo.currentIndex())
                    if exact_item and isinstance(data, tuple) and data[0] == "configuration":
                        service.set_active_configuration(row.id, data[1])
                        continue
                    typed = combo.currentText().strip()
                    if typed and not (exact_item and data is None):
                        service.create_configuration(
                            row.id, typed, configuration_type="weapon",
                            loadout_name=typed, points=None, is_active=True,
                        )
                    else:
                        service.clear_active_configuration(row.id, kind)
                chosen_points = dialog.points.value()
                if dialog.reset_points_requested:
                    service.set_unit_points(row.unit_id, standard_points, manual=False)
                elif chosen_points != standard_points:
                    service.set_unit_points(row.unit_id, chosen_points, manual=True)
                return True
            except CollectionError as exc:
                QMessageBox.warning(self, "Could not edit model", str(exc))
                return False

        def edit_unit(self, models):
            """Edit whole-unit magnetization while keeping model states independent."""
            dialog = QDialog(self)
            dialog.setWindowTitle(f"Edit Unit — {models[0].unit}")
            layout = QVBoxLayout(dialog)
            magnetize_all = QCheckBox("Magnetize entire Unit")
            magnetize_all.setTristate(True)
            if all(model.is_magnetized for model in models):
                magnetize_all.setCheckState(Qt.Checked)
            elif any(model.is_magnetized for model in models):
                magnetize_all.setCheckState(Qt.PartiallyChecked)
            else:
                magnetize_all.setCheckState(Qt.Unchecked)
            unit_form = QFormLayout()
            unit_form.addRow("Magnetized", magnetize_all)

            alternative = QComboBox()
            alternative.setEditable(True)
            alternative.setInsertPolicy(QComboBox.NoInsert)
            alternative.setMaxVisibleItems(20)
            alternative.lineEdit().setPlaceholderText("Search or enter an Alternative Model")
            try:
                unit_profiles = _profiles_for_faction(
                    rules.list_profiles(models[0].game_system), models[0].faction
                )
            except RulesError:
                unit_profiles = []
            for profile in unit_profiles:
                faction = str(profile.get("faction", "")).strip()
                name = str(profile.get("name", "")).strip()
                if not name or name.casefold() == models[0].unit.casefold():
                    continue
                profile_points = profile.get("points")
                unit_size = profile.get("unit_size")
                details = []
                if unit_size not in (None, ""):
                    details.append(f"{unit_size} models")
                if profile_points not in (None, ""):
                    details.append(f"{profile_points} pts")
                label = name + (f"  —  {' · '.join(details)}" if details else "")
                alternative.addItem(label, (faction, name, profile_points, unit_size))
            completer = QCompleter(alternative.model(), alternative)
            completer.setCaseSensitivity(Qt.CaseInsensitive)
            completer.setFilterMode(Qt.MatchContains)
            completer.setCompletionMode(QCompleter.PopupCompletion)
            alternative.setCompleter(completer)
            unit_form.addRow("Alternative Model", alternative)

            active_original = QRadioButton(models[0].unit)
            active_alternative = QRadioButton("Alternative Model")
            active_group = QButtonGroup(dialog)
            active_group.addButton(active_original)
            active_group.addButton(active_alternative)
            active_original.setChecked(True)
            common_active = {
                (model.represented_unit or "").strip() for model in models
                if model.current_model_configuration
            }
            if len(common_active) == 1:
                current_name = next(iter(common_active))
                for index in range(alternative.count()):
                    data = alternative.itemData(index)
                    if data and data[1].casefold() == current_name.casefold():
                        alternative.setCurrentIndex(index)
                        active_alternative.setChecked(True)
                        break
            active_row = QHBoxLayout()
            active_row.addWidget(active_original)
            active_row.addWidget(active_alternative)
            unit_form.addRow("Active", active_row)
            unit_points = QSpinBox()
            unit_points.setRange(0, 999999)
            unit_points.setReadOnly(True)
            base_points = rules.points(models[0].faction, models[0].unit, models[0].game_system)
            if base_points is not None:
                unit_points.setValue(int(base_points))
            unit_form.addRow("Points", unit_points)
            layout.addLayout(unit_form)

            def update_unit_alternative():
                selected_name = alternative.currentText().split("  —  ", 1)[0].strip()
                active_alternative.setText(
                    f"Alternative: {selected_name}" if selected_name else "Alternative Model"
                )
                if active_alternative.isChecked():
                    data = alternative.currentData()
                    if data and data[2] is not None:
                        unit_points.setValue(int(data[2]))

            def update_unit_active():
                if active_original.isChecked() and base_points is not None:
                    unit_points.setValue(int(base_points))
                else:
                    update_unit_alternative()

            alternative.currentIndexChanged.connect(update_unit_alternative)
            active_original.toggled.connect(update_unit_active)
            active_alternative.toggled.connect(
                lambda checked: magnetize_all.setCheckState(Qt.Checked) if checked else None
            )
            update_unit_alternative()
            layout.addWidget(QLabel(
                "The controls above apply the same Alternative and Active choice to every "
                "Physical Model. Use the list below only when individual models differ."
            ))
            model_list = QListWidget()
            for number, model in enumerate(models, 1):
                active = model.represented_unit or model.unit
                model_list.addItem(
                    f"{model.display_name or f'Model {number}'}  —  Active: {active}"
                )
            model_list.setCurrentRow(0)
            model_list.setMinimumWidth(620)
            layout.addWidget(model_list)
            edit_button = QPushButton("Edit selected Physical Model…")
            layout.addWidget(edit_button)

            def edit_selected_unit_model():
                index = model_list.currentRow()
                if index < 0:
                    return
                row = next((item for item in service.list_collection() if item.id == models[index].id), models[index])
                child = ModelDialog(dialog, row)
                if magnetize_all.checkState() == Qt.Checked:
                    child.magnetized.setChecked(True)
                if child.exec() == QDialog.Accepted and self.apply_model_dialog(row, child):
                    refreshed = next(item for item in service.list_collection() if item.id == row.id)
                    models[index] = refreshed
                    model_list.item(index).setText(
                        f"{refreshed.display_name or f'Model {index + 1}'}  —  "
                        f"Active: {refreshed.represented_unit or refreshed.unit}"
                    )

            edit_button.clicked.connect(edit_selected_unit_model)
            model_list.itemDoubleClicked.connect(lambda _item: edit_selected_unit_model())
            buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            if dialog.exec() != QDialog.Accepted:
                self.refresh()
                return
            try:
                if magnetize_all.checkState() != Qt.PartiallyChecked:
                    service.bulk_update_models(
                        [model.id for model in models],
                        is_magnetized=magnetize_all.checkState() == Qt.Checked,
                    )
                if magnetize_all.checkState() == Qt.Unchecked or active_original.isChecked():
                    standard_points = rules.points(
                        models[0].faction, models[0].unit, models[0].game_system
                    )
                    for model in models:
                        service.clear_active_configuration(
                            model.id, "model", unit_points=standard_points
                        )
                else:
                    data = alternative.currentData()
                    exact_item = (
                        alternative.currentIndex() >= 0
                        and alternative.currentText() == alternative.itemText(alternative.currentIndex())
                    )
                    if exact_item and data:
                        faction, model_name, profile_points, _unit_size = data
                    else:
                        faction = models[0].faction
                        model_name = alternative.currentText().strip()
                        profile_points = unit_points.value()
                    if not model_name:
                        raise CollectionError(
                            "Choose or enter an Alternative Model before making it Active"
                        )
                    for model in models:
                        existing = next((
                            item for item in service.list_configurations(model.id)
                            if item.configuration_type == "model"
                            and (item.rule_model_name or item.represented_unit or "").casefold()
                            == model_name.casefold()
                        ), None)
                        if existing:
                            standard_points = rules.points(
                                faction, model_name, model.game_system
                            )
                            service.set_active_configuration(
                                model.id, existing.id, unit_points=standard_points
                            )
                        else:
                            standard_points = rules.points(
                                faction, model_name, model.game_system
                            )
                            service.create_configuration(
                                model.id, model_name, configuration_type="model",
                                rule_faction=faction, rule_model_name=model_name,
                                points=(int(profile_points) if profile_points is not None
                                        else unit_points.value()),
                                is_active=True, unit_points=standard_points,
                            )
            except CollectionError as exc:
                QMessageBox.warning(self, "Could not edit Unit", str(exc))
                return
            self.refresh()

        def bulk_edit(self, selected):
            dialog = QDialog(self)
            dialog.setWindowTitle(f"Bulk edit {len(selected)} models")
            form = QFormLayout(dialog)
            assembly, paint, magnetized = QComboBox(), QComboBox(), QComboBox()
            assembly.addItem("Keep current", None)
            paint.addItem("Keep current", None)
            magnetized.addItem("Keep current", None)
            for value in sorted(ASSEMBLY_STATUSES): assembly.addItem(ASSEMBLY_LABELS[value], value)
            for value in sorted(PAINT_STATUSES): paint.addItem(PAINT_LABELS[value], value)
            magnetized.addItem("Magnetized", True)
            magnetized.addItem("Not magnetized", False)
            keep_location = QRadioButton("Keep current locations")
            custom_location = QRadioButton("Use the same custom location")
            shared_location = QRadioButton("Use the same Cabinet / Slot")
            sequence_location = QRadioButton("Assign Cabinet / Slot in sequence")
            location_modes = QButtonGroup(dialog)
            for button in (keep_location, custom_location, shared_location, sequence_location):
                location_modes.addButton(button)
            keep_location.setChecked(True)
            custom_text = QLineEdit()
            custom_text.setPlaceholderText("Example: Display case A, top shelf")
            cabinet, slot, capacity = QSpinBox(), QSpinBox(), QSpinBox()
            for spin in (cabinet, slot, capacity): spin.setRange(1, 9999)
            cabinet.setValue(1); slot.setValue(1); capacity.setValue(20)
            form.addRow("Assembly", assembly)
            form.addRow("Paint", paint)
            form.addRow("Magnetization", magnetized)
            form.addRow("Location", keep_location)
            form.addRow("", custom_location)
            form.addRow("Custom location", custom_text)
            form.addRow("", shared_location)
            form.addRow("", sequence_location)
            form.addRow("Cabinet X", cabinet)
            form.addRow("Slot Y", slot)
            form.addRow("Slots per cabinet (sequence only)", capacity)
            custom_text.setEnabled(False)
            for spin in (cabinet, slot, capacity): spin.setEnabled(False)
            custom_location.toggled.connect(custom_text.setEnabled)
            shared_location.toggled.connect(cabinet.setEnabled)
            shared_location.toggled.connect(slot.setEnabled)
            sequence_location.toggled.connect(cabinet.setEnabled)
            sequence_location.toggled.connect(slot.setEnabled)
            sequence_location.toggled.connect(capacity.setEnabled)
            buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
            buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
            form.addRow(buttons)
            if dialog.exec() != QDialog.Accepted:
                return
            location = (
                (cabinet.value(), slot.value(), capacity.value())
                if sequence_location.isChecked() else None
            )
            shared = None
            if custom_location.isChecked():
                shared = custom_text.text()
            elif shared_location.isChecked():
                shared = f"Cabinet {cabinet.value()} Slot {slot.value()}"
            try:
                count = service.bulk_update_models(
                    [row.id for row in selected],
                    assembly_status=assembly.currentData(), paint_status=paint.currentData(),
                    is_magnetized=magnetized.currentData(), location_start=location,
                    storage_location=shared,
                )
                if magnetized.currentData() is False:
                    for row in selected:
                        base_points = rules.points(row.faction, row.unit, row.game_system)
                        service.clear_active_configuration(
                            row.id, "model", unit_points=base_points
                        )
            except CollectionError as exc:
                QMessageBox.warning(self, "Could not bulk edit models", str(exc))
                return
            self.refresh()
            QMessageBox.information(self, "Bulk edit complete", f"Updated {count} physical models.")

        def delete_model(self):
            selected = self.selected_rows()
            if not selected:
                QMessageBox.information(self, "Select models", "Select one or more models first.")
                return
            dialog = QDialog(self)
            dialog.setWindowTitle(f"Delete {len(selected)} models?")
            dialog.setModal(True)
            layout = QVBoxLayout(dialog)
            layout.addWidget(QLabel(
                f"Confirm all {len(selected)} highlighted models to delete:"
            ))
            model_list = QListWidget()
            for row in selected:
                model_list.addItem(
                    f"{row.game_system} / {row.faction} / {row.unit} / "
                    f"{row.display_name or '(unnamed model)'}"
                )
            model_list.setSelectionMode(QListWidget.NoSelection)
            model_list.setMinimumWidth(620)
            model_list.setMinimumHeight(min(360, 28 * len(selected) + 12))
            layout.addWidget(model_list)
            layout.addWidget(QLabel(
                "Only the models shown above will be deleted. Cancel leaves the selection unchanged."
            ))
            buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
            delete_button = buttons.addButton("Delete all listed models", QDialogButtonBox.DestructiveRole)
            delete_button.clicked.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            if dialog.exec() != QDialog.Accepted:
                return
            try:
                service.delete_models([row.id for row in selected])
            except CollectionError as exc:
                QMessageBox.warning(self, "Could not delete models", str(exc))
                return
            self.refresh_all()

    class ImportPage(QWidget):
        def __init__(self, collection_page):
            super().__init__()
            self.parsed: ParsedArmy | None = None
            self.preview_snapshot: tuple | None = None
            self.preview_new_units: list[str] = []
            self.preview_overwritten_units: list[str] = []
            layout = QVBoxLayout(self)
            layout.addWidget(QLabel("Paste the plain-text army list exported by the official GW App."))
            form = QFormLayout()
            self.system = QComboBox()
            self.faction = QComboBox()
            self.add_faction_button = QPushButton("Add…")
            system_row = QHBoxLayout()
            system_row.addWidget(self.system, 1)
            faction_row = QHBoxLayout()
            faction_row.addWidget(self.faction, 1)
            faction_row.addWidget(self.add_faction_button)
            form.addRow("Game system", system_row)
            form.addRow("Faction", faction_row)
            layout.addLayout(form)
            self.system.currentIndexChanged.connect(self.refresh_factions)
            self.add_faction_button.clicked.connect(self.add_faction)
            self.source = QTextEdit()
            self.source.setPlaceholderText("Paste GW App export text here…")
            layout.addWidget(self.source, 2)
            buttons = QHBoxLayout()
            preview = QPushButton("Preview")
            self.import_button = QPushButton("Confirm Import")
            self.import_button.setEnabled(False)
            csv_button = QPushButton("Import WMS CSV…")
            preview.clicked.connect(self.preview)
            self.import_button.clicked.connect(lambda: self.import_units(collection_page))
            csv_button.clicked.connect(lambda: self.import_wms_csv(collection_page))
            buttons.addWidget(preview); buttons.addWidget(self.import_button); buttons.addWidget(csv_button); buttons.addStretch()
            layout.addLayout(buttons)
            self.summary = QLabel("No import preview yet.")
            layout.addWidget(self.summary)
            self.table = QTableWidget(0, 2)
            self.table.setHorizontalHeaderLabels(("Unit", "Models"))
            self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            layout.addWidget(self.table, 1)
            self.refresh_systems()
            self.system.currentIndexChanged.connect(self.invalidate_preview)
            self.faction.currentIndexChanged.connect(self.invalidate_preview)
            self.source.textChanged.connect(self.invalidate_preview)

        def invalidate_preview(self, *_):
            self.parsed = None
            self.preview_snapshot = None
            self.preview_new_units = []
            self.preview_overwritten_units = []
            self.preview_missing_points = ()
            self.import_button.setEnabled(False)

        def current_import_snapshot(self):
            system = self.system.currentText()
            rule_path = rules.app_rule_path(system) if system else None
            unit_data_path = rules.pdf_rule_path(system) if system else None
            try:
                rule_digest = hashlib.sha256(rule_path.read_bytes()).hexdigest() if rule_path else ""
            except OSError:
                rule_digest = ""
            try:
                unit_data_digest = hashlib.sha256(unit_data_path.read_bytes()).hexdigest() if unit_data_path else ""
            except OSError:
                unit_data_digest = ""
            return (
                self.system.currentData(), system, self.faction.currentData(),
                self.faction.currentText(),
                hashlib.sha256(self.source.toPlainText().encode("utf-8")).hexdigest(),
                rule_digest, unit_data_digest,
            )

        def fill_combo(self, combo, items, selected_id=None):
            combo.blockSignals(True)
            combo.clear()
            for entity_id, name in items:
                combo.addItem(name, entity_id)
            index = combo.findData(selected_id)
            combo.setCurrentIndex(index if index >= 0 else (0 if combo.count() else -1))
            combo.blockSignals(False)

        def refresh_systems(self, selected_id=None):
            self.fill_combo(self.system, service.list_named("game_system"), selected_id)
            self.refresh_factions()

        def refresh_factions(self, selected_id=None):
            system_id = self.system.currentData()
            items = service.list_named("faction", "game_system_id", system_id) if system_id else []
            self.fill_combo(self.faction, items, selected_id)
            self.add_faction_button.setEnabled(bool(system_id))

        def add_faction(self):
            system_id = self.system.currentData()
            if not system_id:
                QMessageBox.information(self, "Choose a game system", "Add or choose a game system first.")
                return
            name, ok = QInputDialog.getText(self, "Add faction", "Faction name")
            if not ok:
                return
            try:
                entity_id = service.create_faction(system_id, name)
            except CollectionError as exc:
                QMessageBox.warning(self, "Could not add faction", str(exc))
                return
            self.refresh_factions(entity_id)

        def select_name(self, combo, name):
            for index in range(combo.count()):
                if combo.itemText(index).casefold() == name.casefold():
                    combo.setCurrentIndex(index)
                    return True
            return False

        def preview(self):
            self.invalidate_preview()
            target_system = self.system.currentText()
            target_system_id = self.system.currentData()
            if not target_system_id:
                QMessageBox.warning(self, "Choose a game system", "Choose the target Game System first.")
                return False
            try:
                rules.validate_binding(target_system_id, target_system, "app")
                self.parsed = parse_gw_army_text(
                    self.source.toPlainText(),
                    # Detection may call the source format "Warhammer 40,000"
                    # while the selected WMS system is named "40K".  Every
                    # precheck must use the selected/bound target system.
                    lambda detected_system, faction, unit: _target_unit_size(
                        rules, target_system, self.faction.currentText(),
                        detected_system, faction, unit,
                    ),
                    self.system.currentText(), self.faction.currentText(),
                    rules.parser_type(self.system.currentText()),
                    rules.parser_definition(self.system.currentText()),
                )
            except (GWImportError, RulesError) as exc:
                self.parsed = None
                self.table.setRowCount(0)
                parser_name = rules.parser_type(target_system) or "Not configured"
                self.summary.setText(
                    f"Parser failed · Target: {target_system} · Parser: {parser_name}"
                )
                QMessageBox.warning(
                    self, "App Text was not recognized",
                    f"Game System: {target_system}\n"
                    f"Parser: {parser_name}\n\n"
                    f"The selected Parser could not extract valid unit data from this text.\n\n"
                    f"Reason: {exc}\n\n"
                    "Nothing was written to Unit Data or Collection. Check the App export, "
                    "or edit/replace this Game System's Parser Rule in Settings."
                )
                return False
            detected_faction = self.parsed.faction.strip()
            existing_faction = any(
                self.faction.itemText(index).casefold() == detected_faction.casefold()
                for index in range(self.faction.count())
            ) if detected_faction else bool(self.faction.currentData())
            self.table.setRowCount(len(self.parsed.units))
            for index, unit in enumerate(self.parsed.units):
                self.table.setItem(index, 0, QTableWidgetItem(unit.name))
                detail = str(unit.model_count)
                if unit.physical_models:
                    detail += " · " + "; ".join(
                        f"{model.quantity}× {model.name} [{', '.join(model.weapons) or 'No weapons'}]"
                        for model in unit.physical_models
                    )
                self.table.setItem(index, 1, QTableWidgetItem(detail))
            total = sum(unit.model_count for unit in self.parsed.units)
            try:
                _payload, self.preview_new_units, self.preview_overwritten_units = (
                    rules.build_merged_unit_data_preview(target_system, self.parsed)
                )
            except RulesError as exc:
                self.invalidate_preview()
                QMessageBox.warning(self, "Unit Data comparison failed", str(exc))
                return False
            target_faction = detected_faction or self.faction.currentText().strip()
            self.preview_missing_points = _missing_unit_points(
                self.parsed, rules, target_system, target_faction
            )
            summary = (f"Target: {target_system} · Detected: {self.parsed.game_system or 'Generic'} · "
                       f"Found {len(self.parsed.units)} units and {total} physical models. "
                       f"Unit Data: {len(self.preview_new_units)} new, "
                       f"{len(self.preview_overwritten_units)} existing to overwrite.")
            if self.parsed.missing_profiles:
                summary += (
                    f"  Warning: {len(self.parsed.missing_profiles)} units have no Unit Size; "
                    "App Text model counts will be used."
                )
            if self.preview_missing_points:
                summary += f"  Warning: {len(self.preview_missing_points)} units have no Points."
            self.summary.setText(summary)
            self.table.resizeColumnsToContents()
            self.preview_snapshot = self.current_import_snapshot()
            self.import_button.setEnabled(bool(detected_faction or self.faction.currentData()))
            if detected_faction and not existing_faction:
                self.summary.setText(
                    summary + f"  Preview only: Faction '{detected_faction}' will be created after confirmation."
                )
            return True

        def import_units(self, collection_page):
            if self.parsed is None or self.preview_snapshot != self.current_import_snapshot():
                self.invalidate_preview()
                QMessageBox.warning(
                    self, "Preview required",
                    "The source, target, or Parser changed after detection. Run Preview again. No data was changed."
                )
                return
            if not self.system.currentData():
                QMessageBox.warning(
                    self, "Choose a game system",
                    "Choose an existing Game System. New Game Systems can only be created in Settings."
                )
                return
            target_faction = self.parsed.faction.strip() or self.faction.currentText().strip()
            if not target_faction:
                QMessageBox.warning(self, "Choose a faction", "The Parser did not detect a Faction and none is selected.")
                return
            if self.parsed.missing_profiles:
                missing_dialog = UnitListConfirmationDialog(
                    self, "Unit Size missing",
                    "These Units have no Unit Size in the selected Game System's Unit Data. "
                    "App Text model counts will be used. Continue to Unit selection?",
                    [
                        {"key": name, "action": "Missing", "faction": target_faction,
                         "unit": name, "change": "Using App Text model count"}
                        for name in self.parsed.missing_profiles
                    ],
                    "Continue", False,
                )
                if missing_dialog.exec() != QDialog.Accepted:
                    return
            if self.preview_missing_points:
                missing_dialog = UnitListConfirmationDialog(
                    self, "Points missing",
                    "These Units have no Points in the selected Game System's Unit Data or "
                    "the confirmed App Text preview. They will be imported with '-' Points.",
                    [
                        {"key": name, "action": "Missing", "faction": target_faction,
                         "unit": name, "change": "Points will be '-'"}
                        for name in self.preview_missing_points
                    ],
                    "Continue", False,
                )
                if missing_dialog.exec() != QDialog.Accepted:
                    return
            new_keys = {name.casefold() for name in self.preview_new_units}
            rows = []
            for index, unit in enumerate(self.parsed.units):
                action = "New" if unit.name.casefold() in new_keys else "Overwrite"
                rows.append({
                    "key": index, "action": action, "faction": target_faction,
                    "unit": unit.name,
                    "change": f"{unit.model_count} Physical Models",
                })
            dialog = UnitListConfirmationDialog(
                self, "Confirm detected import",
                f"Game System: {self.system.currentText()} · Faction: {target_faction} · "
                f"Parser: {rules.parser_type(self.system.currentText())}\n"
                "Selected Units will update Unit Data and create Collection Unit Instances. "
                "Unselected Units will not be changed or imported.", rows,
                "Confirm Selected Units",
            )
            if dialog.exec() != QDialog.Accepted:
                return
            selected_indexes = dialog.selected_keys()
            selected_units = tuple(
                unit for index, unit in enumerate(self.parsed.units)
                if index in selected_indexes
            )
            selected_parsed = ParsedArmy(
                target_faction, selected_units, self.parsed.game_system,
                tuple(name for name in self.parsed.missing_profiles
                      if any(unit.name == name for unit in selected_units)),
                self.parsed.detected_format,
            )
            quantity_rows, physical_model_total = _import_quantity_rows(
                selected_units, target_faction
            )
            quantity_dialog = UnitListConfirmationDialog(
                self, "Confirm physical model quantities",
                f"The selected {len(selected_units)} Unit entries will create "
                f"{physical_model_total} Physical Models in Collection. "
                "Confirm these detected quantities before any data is written.",
                quantity_rows,
                f"Import {physical_model_total} Physical Models",
                False,
            )
            if quantity_dialog.exec() != QDialog.Accepted:
                return
            # Recheck the immutable Preview immediately before the first write.
            if self.preview_snapshot != self.current_import_snapshot():
                self.invalidate_preview()
                QMessageBox.warning(self, "Import blocked", "The target or Parser changed. Run Preview again.")
                return
            unit_data_path = rules.pdf_rule_path(self.system.currentText())
            change_log_path = unit_data_path.parent / "change_log.jsonl"
            original_unit_data = None
            original_change_log = None
            try:
                original_unit_data = (
                    unit_data_path.read_bytes() if unit_data_path.exists() else None
                )
                original_change_log = (
                    change_log_path.read_bytes() if change_log_path.exists() else None
                )
                merged_payload, new_units, overwritten_units = rules.build_merged_unit_data_preview(
                    self.system.currentText(), selected_parsed
                )
                rules.save_rule_json(
                    self.system.currentText(), "pdf", merged_payload, "Parser extraction",
                    f"App Text Unit Data import: {len(new_units)} new, "
                    f"{len(overwritten_units)} overwritten",
                    import_method="App Text", new_units=len(new_units),
                    overwritten_units=len(overwritten_units),
                )
                # Unit Data is the single source of truth for Unit Points for
                # every Game System.  App/PDF source values are never written
                # directly to Collection; they must first survive Preview and
                # be committed to Unit Data above.
                import_data = _resolve_unit_instance_points(
                    selected_parsed, rules, self.system.currentText(), target_faction
                )
                count = service.import_army(
                    self.system.currentText(), target_faction, import_data.units,
                    self.system.currentData(),
                )
            except Exception as exc:
                try:
                    if original_unit_data is None:
                        unit_data_path.unlink(missing_ok=True)
                    else:
                        unit_data_path.write_bytes(original_unit_data)
                    if original_change_log is None:
                        change_log_path.unlink(missing_ok=True)
                    else:
                        change_log_path.write_bytes(original_change_log)
                except OSError as restore_exc:
                    QMessageBox.critical(self, "Import rollback failed", f"Collection was rolled back, but Unit Data restoration failed:\n{restore_exc}")
                    return
                QMessageBox.warning(
                    self, "Could not import army",
                    f"{type(exc).__name__}: {exc}\n\n"
                    "Collection and Unit Data were rolled back. No Physical Models were added."
                )
                return
            self.invalidate_preview()
            collection_page.refresh_all()
            QMessageBox.information(self, "Import complete", f"Added {count} physical models to Collection.")

        def import_wms_csv(self, collection_page):
            filename, _ = QFileDialog.getOpenFileName(
                self, "Import WMS Collection CSV", "", "CSV files (*.csv)"
            )
            if not filename:
                return
            try:
                rows = _read_wms_collection_csv(filename)
                unit_count, model_count = _validate_wms_collection_rows(service, rows)
            except (CollectionError, OSError, csv.Error) as exc:
                QMessageBox.warning(
                    self, "CSV import blocked", f"{exc}\n\nNo database record was changed."
                )
                return
            answer = QMessageBox.question(
                self, "Confirm validated WMS CSV import",
                f"Validation: Passed\nUnit instances: {unit_count}\nPhysical models: {model_count}\n\n"
                "Import adds new Collection records; it does not overwrite existing records. Continue?",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                return
            # Re-read and revalidate after confirmation, then retain a recoverable DB snapshot.
            try:
                confirmed_rows = _read_wms_collection_csv(filename)
                if confirmed_rows != rows:
                    raise CollectionError("The CSV changed after validation. Run the import again.")
                _validate_wms_collection_rows(service, confirmed_rows)
                snapshot_handle = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
                snapshot_handle.close()
                snapshot_path = Path(snapshot_handle.name)
                with service.database.connect() as source_db, sqlite3.connect(snapshot_path) as snapshot_db:
                    source_db.backup(snapshot_db)
            except (CollectionError, OSError, sqlite3.Error, csv.Error) as exc:
                QMessageBox.warning(self, "CSV import blocked", f"{exc}\n\nNo database record was changed.")
                return
            try:
                units, models = _import_wms_collection_csv(service, filename)
            except (CollectionError, OSError, csv.Error, ValueError) as exc:
                try:
                    with sqlite3.connect(snapshot_path) as snapshot_db, service.database.connect() as target_db:
                        snapshot_db.backup(target_db)
                except sqlite3.Error as restore_exc:
                    QMessageBox.critical(self, "CSV rollback failed", f"{exc}\n\nDatabase restoration failed: {restore_exc}")
                    snapshot_path.unlink(missing_ok=True)
                    return
                snapshot_path.unlink(missing_ok=True)
                QMessageBox.warning(self, "CSV import failed", f"{exc}\n\nThe database was restored to its pre-import state.")
                return
            snapshot_path.unlink(missing_ok=True)
            refresh_game_system_views()
            collection_page.refresh_all()
            QMessageBox.information(
                self, "CSV import complete", f"Added {units} Unit instances and {models} Physical models."
            )

    class RuleJsonManagerDialog(QDialog):
        def __init__(self, system, parent=None):
            super().__init__(parent)
            self.system = system
            self.setWindowTitle(f"View JSON — {system}")
            self.resize(980, 680)
            layout = QVBoxLayout(self)
            title = QLabel("VIEW PARSER AND UNIT DATA JSON")
            title.setStyleSheet("font-size: 20px; font-weight: 700; color: #ffffff;")
            layout.addWidget(title)
            layout.addWidget(QLabel(
                "Read-only view. Replace JSON through the corresponding Settings action."
            ))
            tabs = QTabWidget()
            self.parser_json = QTextEdit()
            self.unit_json = QTextEdit()
            for editor in (self.parser_json, self.unit_json):
                editor.setReadOnly(True)
                editor.setFontFamily("Consolas")
            tabs.addTab(self.parser_json, "Parser")
            tabs.addTab(self.unit_json, "Unit Data")
            layout.addWidget(tabs, 1)
            buttons = QDialogButtonBox(QDialogButtonBox.Close)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)
            self.load()

        def load(self, *_):
            try:
                parser = rules.load_rule_json(self.system, "app")
                unit_data = rules.load_rule_json(self.system, "pdf")
            except RulesError as exc:
                QMessageBox.warning(self, "Could not load JSON", str(exc)); return
            self.parser_json.setPlainText(json.dumps(parser, indent=2, ensure_ascii=False))
            self.unit_json.setPlainText(json.dumps(unit_data, indent=2, ensure_ascii=False))

    class SettingsPage(QWidget):
        def __init__(self):
            super().__init__()
            layout = QVBoxLayout(self)
            layout.addWidget(QLabel(
                "Game Systems, Parsers, and Unit Data are managed separately. "
                "All JSON imports are validated before replacement."
            ))

            system_group = QGroupBox("1. Game System")
            system_layout = QVBoxLayout(system_group)
            form = QFormLayout()
            self.system = QComboBox()
            self.aos_url = QLineEdit()
            self.aos_url.setReadOnly(True)
            self.database_file = QLineEdit(str(service.database.path))
            self.database_file.setReadOnly(True)
            self.app_rule_file = QLineEdit(); self.app_rule_file.setReadOnly(True)
            self.pdf_rule_file = QLineEdit(); self.pdf_rule_file.setReadOnly(True)
            form.addRow("Game system", self.system)
            form.addRow("Unit Data Source URL", self.aos_url)
            form.addRow("Physical model database", self.database_file)
            system_layout.addLayout(form)
            system_buttons = QHBoxLayout()
            create_system = QPushButton("Create Game System…")
            delete_models = QPushButton("Delete all models in this system…")
            delete_system = QPushButton("Delete this game system…")
            create_system.clicked.connect(self.create_game_system)
            delete_models.clicked.connect(self.delete_system_models)
            delete_system.clicked.connect(self.delete_game_system)
            for button in (create_system, delete_models, delete_system):
                system_buttons.addWidget(button)
            system_buttons.addStretch()
            system_layout.addLayout(system_buttons)
            layout.addWidget(system_group)

            parser_group = QGroupBox("2. Parser")
            parser_layout = QVBoxLayout(parser_group)
            parser_form = QFormLayout()
            parser_form.addRow("Parser JSON", self.app_rule_file)
            parser_layout.addLayout(parser_form)
            parser_buttons = QHBoxLayout()
            self.app_parser_button = QPushButton("Create / Update Parser from App Text…")
            self.pdf_parser_button = QPushButton("Create / Update Parser from PDF…")
            self.replace_parser_button = QPushButton("Replace Parser JSON…")
            self.test_new_parser_button = QPushButton("Test Parser…")
            parser_view_json = QPushButton("View JSON…")
            export_parser = QPushButton("Export Parser JSON…")
            remove_parser = QPushButton("Remove Parser…")
            self.app_parser_button.clicked.connect(lambda: self.generate_rule("App Text"))
            self.pdf_parser_button.clicked.connect(lambda: self.generate_rule("PDF"))
            self.test_new_parser_button.clicked.connect(self.test_new_parser)
            self.replace_parser_button.clicked.connect(lambda: self.import_rule("app"))
            parser_view_json.clicked.connect(self.manage_rule_json)
            export_parser.clicked.connect(lambda: self.export_json("app"))
            remove_parser.clicked.connect(lambda: self.remove_json("app"))
            for button in (self.app_parser_button, self.pdf_parser_button,
                           self.replace_parser_button, self.test_new_parser_button,
                           parser_view_json, export_parser, remove_parser):
                parser_buttons.addWidget(button)
            parser_buttons.addStretch()
            parser_layout.addLayout(parser_buttons)
            layout.addWidget(parser_group)

            unit_group = QGroupBox("3. Unit Data")
            unit_layout = QVBoxLayout(unit_group)
            unit_form = QFormLayout()
            unit_form.addRow("Unit Data JSON", self.pdf_rule_file)
            unit_layout.addLayout(unit_form)
            unit_buttons = QHBoxLayout()
            update_pdf = QPushButton("Update Unit Data from PDF…")
            update_app = QPushButton("Update Unit Data from App Text…")
            unit_view_json = QPushButton("View JSON…")
            import_units = QPushButton("Replace Unit Data JSON…")
            export_units = QPushButton("Export Unit Data JSON…")
            remove_units = QPushButton("Remove Unit Data…")
            update_pdf.clicked.connect(self.update_rules)
            update_app.clicked.connect(self.update_unit_data_from_app_text)
            unit_view_json.clicked.connect(self.manage_rule_json)
            import_units.clicked.connect(lambda: self.import_rule("pdf"))
            export_units.clicked.connect(lambda: self.export_json("pdf"))
            remove_units.clicked.connect(lambda: self.remove_json("pdf"))
            for button in (update_app, update_pdf, import_units,
                           unit_view_json, export_units, remove_units):
                unit_buttons.addWidget(button)
            unit_buttons.addStretch()
            unit_layout.addLayout(unit_buttons)
            layout.addWidget(unit_group)
            self.status = QLabel("")
            self.status.setWordWrap(True)
            layout.addWidget(self.status)
            self.details = QLabel("")
            self.details.setWordWrap(True)
            layout.addWidget(self.details)
            layout.addWidget(QLabel("Unit Data Import History"))
            self.import_history = QTreeWidget()
            self.import_history.setColumnCount(4)
            self.import_history.setHeaderLabels(("Time", "Method", "New Units", "Overwritten Units"))
            self.import_history.setRootIsDecorated(False)
            self.import_history.setEditTriggers(QTreeWidget.NoEditTriggers)
            self.import_history.setMinimumHeight(125)
            layout.addWidget(self.import_history)
            layout.addStretch()
            self.system.currentTextChanged.connect(self.refresh_system)
            self.refresh_systems()
            self.refresh_system()

        def create_game_system(self):
            name, ok = QInputDialog.getText(
                self, "Create Game System", "Game System name"
            )
            if not ok or not name.strip():
                return
            name = name.strip()
            try:
                system_id = service.create_game_system(name)
                rules.bind_game_system(system_id, name)
            except (CollectionError, RulesError) as exc:
                QMessageBox.warning(self, "Could not create Game System", str(exc))
                return
            refresh_game_system_views(system_id, name)
            QMessageBox.information(
                self, "Game System created",
                f"Created {name}. Parser Rule and Unit Data can now be configured in Settings."
            )

        def manage_rule_json(self):
            if not self.system.currentText():
                QMessageBox.information(self, "No game system", "Create or select a Game System first.")
                return
            RuleJsonManagerDialog(self.system.currentText(), self).exec()
            self.refresh_system()

        def refresh_systems(self, selected_name=None):
            """Reload real database IDs so Settings never uses a stale placeholder."""
            selected = selected_name or self.system.currentText()
            self.system.blockSignals(True)
            self.system.clear()
            for entity_id, name in service.list_named("game_system"):
                self.system.addItem(name, entity_id)
            index = self.system.findText(selected, Qt.MatchFixedString)
            self.system.setCurrentIndex(index if index >= 0 else (0 if self.system.count() else -1))
            self.system.blockSignals(False)
            self.refresh_system()

        def refresh_system(self):
            system = self.system.currentText()
            if not system:
                self.aos_url.clear()
                self.app_rule_file.clear(); self.pdf_rule_file.clear()
                self.status.setText("No Game System is currently defined.")
                self.import_history.clear()
                return
            self.aos_url.setText(rules.source_url(system))
            rules.ensure_game_system(system)
            rules.bind_game_system(self.system.currentData(), system)
            self.app_rule_file.setText(str(rules.app_rule_path(system)))
            self.pdf_rule_file.setText(str(rules.pdf_rule_path(system)))
            self.status.setText(rules.status(system))
            try:
                parser = rules.load_rule_json(system, "app")
                unit_data = rules.load_rule_json(system, "pdf")
                app_definition = rules.parser_definition(system, "app_text")
                pdf_definition = rules.parser_definition(system, "pdf")
                app_ready = bool(app_definition.get("type") or app_definition.get("dialect"))
                pdf_ready = bool(pdf_definition.get("type") or pdf_definition.get("dialect"))
                test_status = parser.get("validation_status", {})
                if isinstance(test_status, str):
                    test_status = {"app_text": test_status}
                app_tested = test_status.get("app_text") == "tested"
                pdf_tested = test_status.get("pdf") == "tested"
                self.replace_parser_button.setVisible(app_ready or pdf_ready)
                self.test_new_parser_button.setVisible(
                    (app_ready and not app_tested) or (pdf_ready and not pdf_tested)
                )
                self.details.setText(
                    f"App Text Parser: {app_definition.get('type') or 'Not configured'} "
                    f"({'Tested' if app_tested else 'Not tested'}) · "
                    f"PDF Parser: {pdf_definition.get('type') or 'Not configured'} "
                    f"({'Tested' if pdf_tested else 'Not tested'}) · "
                    f"Binding: {parser.get('game_system_id') or 'Not bound'} · "
                    f"Unit Profiles: {len(unit_data.get('profiles', []))} · "
                    f"Parser and Unit Data are exclusive to this Game System."
                )
            except RulesError as exc:
                self.test_new_parser_button.setVisible(True)
                self.details.setText(str(exc))
            self.import_history.clear()
            for entry in reversed(rules.unit_data_import_history(system)):
                timestamp = str(entry.get("timestamp", "")).replace("T", " ").replace("+00:00", " UTC")
                self.import_history.addTopLevelItem(QTreeWidgetItem((
                    timestamp,
                    str(entry.get("import_method", "")),
                    str(entry.get("new_units", 0)),
                    str(entry.get("overwritten_units", 0)),
                )))
            for column in range(4):
                self.import_history.resizeColumnToContents(column)

        def test_new_parser(self):
            """Test the saved Parser in memory, then record its validation status."""
            system = self.system.currentText().strip()
            if not system or not self.system.currentData():
                QMessageBox.information(self, "No game system", "Create and select a Game System first.")
                return
            payload = rules.load_rule_json(system, "app")
            status = payload.get("validation_status", {})
            if isinstance(status, str):
                status = {"app_text": status}
            available = []
            if rules.parser_type(system):
                available.append(("App Text Parser", "app_text"))
            pdf_definition = rules.parser_definition(system, "pdf")
            if pdf_definition.get("type") or pdf_definition.get("dialect"):
                available.append(("PDF Parser", "pdf"))
            untested = [item for item in available if status.get(item[1]) != "tested"]
            if not untested:
                QMessageBox.information(self, "No Parser", "Create or import a Parser first.")
                return
            labels = [label for label, _source in untested]
            label, ok = QInputDialog.getItem(self, "Test Parser", "Parser to test", labels, 0, False)
            if not ok:
                return
            source = next(value for title, value in untested if title == label)
            if source == "pdf":
                filename, _ = QFileDialog.getOpenFileName(
                    self, "Choose a representative PDF", "", "PDF files (*.pdf)"
                )
                if not filename:
                    return
                try:
                    _definition, publication, count = rules.inspect_pdf_parser(system, Path(filename))
                except RulesError as exc:
                    QMessageBox.warning(self, "Parser test failed", str(exc))
                    return
                status["pdf"] = "tested"
                payload["validation_status"] = status
                rules.save_rule_json(system, "app", payload, "PDF Parser tested",
                                     f"Recognized {count} Unit Profiles from {publication}")
                self.refresh_system()
                QMessageBox.information(
                    self, "Parser test preview",
                    f"Publication: {publication}\nUnit Profiles: {count}\n\n"
                    "Test only. Unit Data and Collection were not changed."
                )
                return
            sample, ok = QInputDialog.getMultiLineText(
                self, "Test New Parser", "Paste representative App Text. This test will not save anything."
            )
            if not ok:
                return
            try:
                parsed = parse_gw_army_text(
                    sample, lambda gs, faction, unit: rules.unit_size(faction, unit, gs),
                    system, "", rules.parser_type(system), rules.parser_definition(system),
                )
            except GWImportError as exc:
                QMessageBox.warning(
                    self, "Parser test failed",
                    f"No valid Unit data was detected.\n\nReason: {exc}\n\nNo file or database record was changed."
                )
                return
            model_count = sum(unit.model_count for unit in parsed.units)
            status["app_text"] = "tested"
            payload["validation_status"] = status
            rules.save_rule_json(
                system, "app", payload, "Parser tested",
                f"Recognized {len(parsed.units)} Unit entries and {model_count} Physical Models",
            )
            self.refresh_system()
            QMessageBox.information(
                self, "Parser test preview",
                f"Detected format: {parsed.game_system or 'Generic'}\n"
                f"Faction: {parsed.faction or 'Not detected'}\n"
                f"Units: {len(parsed.units)}\nPhysical Models: {model_count}\n\n"
                "Test only. No Parser, Unit Data, or Collection record was saved."
            )

        def update_unit_data_from_app_text(self):
            """Detect and confirm an App Text Unit Data update without adding Collection models."""
            system = self.system.currentText().strip()
            if not system or not self.system.currentData():
                QMessageBox.information(
                    self, "No game system", "Create and select a Game System first."
                )
                return
            sample, ok = QInputDialog.getMultiLineText(
                self, "Update Unit Data from App Text",
                "Paste App Text. Detection and comparison happen before any file is changed."
            )
            if not ok or not sample.strip():
                return
            try:
                parsed = parse_gw_army_text(
                    sample,
                    lambda gs, faction, unit: rules.unit_size(faction, unit, gs),
                    system, "", rules.parser_type(system),
                    rules.parser_definition(system, "app_text"),
                )
                merged, added_names, updated_names = rules.build_merged_unit_data_preview(
                    system, parsed
                )
            except (GWImportError, RulesError) as exc:
                QMessageBox.warning(
                    self, "App Text import blocked",
                    f"The input could not be validated with the selected Game System Parser.\n\n"
                    f"Reason: {exc}\n\nNo file or database record was changed."
                )
                return
            added = len(added_names)
            updated = len(updated_names)
            rows = [
                {"key": ("new", name.casefold()), "action": "New", "faction": parsed.faction,
                 "unit": name, "change": "Add to Unit Data"}
                for name in added_names
            ] + [
                {"key": ("overwrite", name.casefold()), "action": "Overwrite", "faction": parsed.faction,
                 "unit": name, "change": "Replace matching Unit Data fields"}
                for name in updated_names
            ]
            dialog = UnitListConfirmationDialog(
                self, "Confirm App Text Unit Data update",
                f"Game System: {system} · Source: App Text · "
                f"{added} new · {updated} overwrite\nCollection will not be changed.", rows,
            )
            if dialog.exec() != QDialog.Accepted:
                return
            selected = dialog.selected_keys()
            new_keys = {name.casefold() for kind, name in selected if kind == "new"}
            overwrite_keys = {name.casefold() for kind, name in selected if kind == "overwrite"}
            selected_units = tuple(
                unit for unit in parsed.units
                if unit.name.casefold() in (new_keys | overwrite_keys)
            )
            selected_parsed = ParsedArmy(
                parsed.faction, selected_units, parsed.game_system,
                parsed.missing_profiles, parsed.detected_format,
            )
            merged, selected_added, selected_updated = rules.build_merged_unit_data_preview(
                system, selected_parsed
            )
            added, updated = len(selected_added), len(selected_updated)
            try:
                rules.save_rule_json(
                    system, "pdf", merged, "Confirmed App Text Unit Data update",
                    f"App Text Unit Data import: {added} new, {updated} overwritten",
                    import_method="App Text", new_units=added,
                    overwritten_units=updated,
                )
            except RulesError as exc:
                QMessageBox.warning(
                    self, "App Text import failed",
                    f"{exc}\n\nThe existing Unit Data was not replaced."
                )
                return
            self.refresh_system()
            QMessageBox.information(
                self, "Unit Data updated",
                f"Added {added} new Unit profiles and updated {updated} existing profiles. "
                "Collection was not changed."
            )

        def update_rules(self):
            source = choose_pdf_source(
                self, "Scan PDF and Update Unit Data", self.aos_url.text()
            )
            if source is None:
                return
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                source_kind, source_value = source
                if source_kind == "file":
                    payload, count = rules.inspect_rules_file(
                        self.system.currentText(), source_value
                    )
                else:
                    payload, count = rules.inspect_rules_update(
                        self.system.currentText(), source_value
                    )
            except RulesError as exc:
                QMessageBox.warning(self, "Rules update failed", str(exc))
                return
            finally:
                QApplication.restoreOverrideCursor()
            factions = len({str(profile.get("faction", "")) for profile in payload.get("profiles", [])})
            try:
                merged_payload, added_names, updated_names = rules.build_pdf_unit_data_merge_preview(
                    self.system.currentText(), payload
                )
                added, updated = len(added_names), len(updated_names)
                existing_count = len(rules.load_rule_json(
                    self.system.currentText(), "pdf"
                ).get("profiles", []))
            except RulesError as exc:
                QMessageBox.warning(self, "Unit Data comparison failed", str(exc))
                return
            incoming_by_name = {}
            for profile in payload.get("profiles", []):
                incoming_by_name.setdefault(str(profile.get("name", "")).casefold(), profile)
            rows = []
            for action, names in (("New", added_names), ("Overwrite", updated_names)):
                for name in names:
                    profile = incoming_by_name.get(name.casefold(), {})
                    rows.append({
                        "key": (action.casefold(), name.casefold()), "action": action,
                        "faction": profile.get("faction", ""), "unit": name,
                        "change": f"{profile.get('unit_size', '?')} models · "
                                  f"{profile.get('points', '')} pts",
                    })
            dialog = UnitListConfirmationDialog(
                self, "Confirm PDF Unit Data update",
                f"Game System: {self.system.currentText()} · Source: PDF · "
                f"Publication: {payload.get('publication', 'Unknown')}\n"
                f"{factions} factions · {existing_count} existing profiles · "
                f"{added} new · {updated} overwrite\n"
                "Merge Selected keeps profiles not selected and existing App-only details. "
                "Replace All uses the complete detected PDF and discards profiles absent from it.",
                rows, "Merge Selected", True, "Replace All Unit Data",
            )
            if dialog.exec() != QDialog.Accepted:
                self.status.setText("PDF detection completed; Preview discarded. No file was changed.")
                return
            replace_all = dialog.selected_action == "secondary"
            if replace_all:
                selected_payload = payload
                selected_added, selected_updated = added, updated
            else:
                selected_names = {name for _action, name in dialog.selected_keys()}
                selected_profiles = [
                    profile for profile in payload.get("profiles", [])
                    if str(profile.get("name", "")).casefold() in selected_names
                ]
                selected_source = dict(payload)
                selected_source["profiles"] = selected_profiles
                selected_payload, selected_added_names, selected_updated_names = (
                    rules.build_pdf_unit_data_merge_preview(
                        self.system.currentText(), selected_source
                    )
                )
                selected_added = len(selected_added_names)
                selected_updated = len(selected_updated_names)
            try:
                rules.save_rule_json(
                    self.system.currentText(), "pdf", selected_payload, "Confirmed PDF scan",
                    (f"Merged PDF Unit Data: {selected_added} added, {selected_updated} updated"
                     if not replace_all else
                     f"Replaced all Unit Data with {count} validated PDF profiles"),
                    import_method="PDF", new_units=selected_added,
                    overwritten_units=selected_updated,
                )
                if source_kind == "url":
                    rules.set_source_url(self.system.currentText(), str(source_value))
            except RulesError as exc:
                QMessageBox.warning(self, "Rules update failed", f"{exc}\n\nThe existing Unit Data was not replaced.")
                return
            self.status.setText(rules.status(self.system.currentText()))
            if source_kind == "url":
                self.aos_url.setText(str(source_value))
            final_count = len(selected_payload.get("profiles", []))
            QMessageBox.information(
                self, "Unit Data updated",
                f"Unit Data now contains {final_count} profiles. Collection was not changed."
            )

        def generate_rule(self, method=None):
            system = self.system.currentText().strip()
            if not system or not self.system.currentData():
                QMessageBox.information(
                    self, "No game system",
                    "Create and select a Game System in Settings before generating a Parser Rule."
                )
                return
            if method not in {"App Text", "PDF"}:
                method, ok = QInputDialog.getItem(
                    self, "Create Parser", "Generate Parser from",
                    ["App Text", "PDF"], 0, False,
                )
                if not ok:
                    return
            if method == "PDF":
                source = choose_pdf_source(
                    self, "Choose a representative PDF", self.aos_url.text()
                )
                if source is None:
                    return
                try:
                    source_kind, source_value = source
                    if source_kind == "file":
                        definition, publication, count = rules.inspect_pdf_parser(
                            system, source_value
                        )
                    else:
                        definition, publication, count = rules.inspect_pdf_parser_url(
                            system, str(source_value)
                        )
                except RulesError as exc:
                    QMessageBox.warning(self, "Could not generate PDF Parser", str(exc))
                    return
                answer = QMessageBox.question(
                    self, "Confirm PDF Parser Preview",
                    f"Target Game System: {system}\nParser: {definition.get('type')}\n"
                    f"Publication: {publication}\nUnit Profiles detected: {count}\n\n"
                    "Save this PDF Parser? Unit Data and Collection will not be changed.",
                    QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
                )
                if answer != QMessageBox.Yes:
                    return
                payload = rules.load_rule_json(system, "app")
                sources = payload.get("parsers", {})
                if not isinstance(sources, dict):
                    sources = {}
                sources["pdf"] = definition
                payload["parsers"] = sources
                statuses = payload.get("validation_status", {})
                if isinstance(statuses, str):
                    statuses = {"app_text": statuses}
                statuses["pdf"] = "tested"
                payload["validation_status"] = statuses
                rules.save_rule_json(system, "app", payload, "Created PDF Parser",
                                     f"Generated from {count} validated PDF Unit Profiles")
                self.refresh_system()
                QMessageBox.information(self, "PDF Parser created", "PDF Parser saved. Unit Data was not changed.")
                return
            sample, ok = QInputDialog.getMultiLineText(
                self, "Create Parser Rule",
                "Paste a representative official App export. Detection will be previewed before saving."
            )
            if not ok:
                return
            try:
                payload, count = rules.inspect_text_rule(system, sample)
            except (RulesError, GWImportError) as exc:
                QMessageBox.warning(self, "Could not generate rule", str(exc))
                return
            parser = payload.get("parser", {})
            answer = QMessageBox.question(
                self, "Confirm Parser Rule Preview",
                f"Target Game System: {system}\n"
                f"Parser dialect: {parser.get('dialect') or parser.get('type')}\n"
                f"Detected Faction: {payload.get('sample', {}).get('detected_faction') or 'Not detected'}\n"
                f"Sample Unit entries: {count}\n\nSave and bind this Parser Rule?",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                return
            try:
                statuses = payload.get("validation_status", {})
                if isinstance(statuses, str):
                    statuses = {}
                statuses["app_text"] = "tested"
                payload["validation_status"] = statuses
                rules.save_rule_json(
                    system, "app", payload, "Created Parser Rule",
                    f"Generated from {count} validated sample Unit entries",
                )
            except RulesError as exc:
                QMessageBox.warning(self, "Could not save Parser Rule", str(exc))
                return
            self.refresh_system()
            QMessageBox.information(
                self, "Parser created",
                f"Saved and tested a Parser using {count} recognized Unit entries.",
            )

        def export_json(self, source):
            system = self.system.currentText().strip()
            if not system:
                return
            source_name = "Parser" if source == "app" else "Unit Data"
            original = rules.rule_path(system, source)
            filename, _ = QFileDialog.getSaveFileName(
                self, f"Export {source_name} JSON", original.name, "JSON files (*.json)"
            )
            if not filename:
                return
            try:
                Path(filename).write_bytes(original.read_bytes())
            except OSError as exc:
                QMessageBox.warning(self, "Export failed", str(exc))
                return
            QMessageBox.information(self, "Export complete", f"Exported {source_name} JSON.")

        def remove_json(self, source):
            system = self.system.currentText().strip()
            if not system:
                return
            source_name = "Parser" if source == "app" else "Unit Data"
            answer = QMessageBox.warning(
                self, f"Remove {source_name}?",
                f"Remove the current {source_name} from {system}?\n\n"
                "Collection records will not be deleted.",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                return
            current = rules.load_rule_json(system, source)
            if source == "app":
                payload = {
                    "format_version": current.get("format_version", 4),
                    "document_type": "wms_import_rule",
                    "game_system_id": current.get("game_system_id"),
                    "game_system_name": system,
                    "game_system": system,
                    "parsers": {"app_text": {}, "pdf": {}},
                    "parser": {"type": ""},
                    "validation_status": "not_configured",
                }
            else:
                payload = {
                    "format_version": current.get("format_version", 4),
                    "document_type": "wms_official_unit_data",
                    "game_system_id": current.get("game_system_id"),
                    "game_system_name": system,
                    "game_system": system,
                    "publication": "Not scanned",
                    "source_url": "",
                    "profiles": [],
                }
            try:
                rules.save_rule_json(system, source, payload, f"Removed {source_name}", "Reset by user")
                if source == "pdf":
                    rules.set_source_url(system, "")
            except RulesError as exc:
                QMessageBox.warning(self, "Remove failed", str(exc))
                return
            self.refresh_system()

        def import_rule(self, rule_source="app"):
            systems = [name for _, name in service.list_named("game_system")]
            if not systems:
                QMessageBox.information(self, "No game system", "Create a Game System before importing a rule.")
                return
            current = self.system.currentText()
            initial = systems.index(current) if current in systems else 0
            system, ok = QInputDialog.getItem(
                self, "Import Rule", "Target Game System", systems, initial, False
            )
            if not ok:
                return
            source_label = "Parser Rule JSON" if rule_source == "app" else "Official Unit Data JSON"
            filename, _ = QFileDialog.getOpenFileName(
                self, f"Choose JSON rule for {system}", "", "JSON rule files (*.json)"
            )
            if not filename:
                return
            try:
                payload, count = rules.inspect_rule_file(system, Path(filename), rule_source)
            except RulesError as exc:
                QMessageBox.warning(
                    self, "Import blocked",
                    f"The selected JSON did not pass validation:\n\n{exc}\n\nNo file or database record was changed."
                )
                return
            parser_type = payload.get("parser", {}).get("type", "") if isinstance(payload.get("parser"), dict) else ""
            summary = (
                f"Target Game System: {system}\n"
                f"Document type: {payload.get('document_type')}\n"
                f"Game System ID: {payload.get('game_system_id')}\n"
                f"Parser: {parser_type or 'Not configured'}\n"
                f"Unit Profiles: {count}\n\n"
                f"This will replace only the current {source_label}. Continue?"
            )
            answer = QMessageBox.warning(
                self, "Confirm validated JSON import", summary,
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                return
            # Validate again after confirmation so a changed source cannot reuse an old Preview.
            try:
                confirmed_payload, confirmed_count = rules.inspect_rule_file(
                    system, Path(filename), rule_source
                )
                if confirmed_payload != payload or confirmed_count != count:
                    raise RulesError("The selected file changed after validation. Run the import again.")
                if rule_source == "app":
                    confirmed_payload["validation_status"] = "not_tested"
                saved_path = rules.commit_inspected_rule(
                    system, confirmed_payload, rule_source, Path(filename).name
                )
            except RulesError as exc:
                QMessageBox.warning(self, "Import blocked", f"{exc}\n\nNo rule was replaced.")
                return
            self.refresh_systems(system)
            QMessageBox.information(
                self,
                "Import rule saved",
                f"Imported {count} unit profiles for {system}.\n\nSaved as:\n{saved_path}",
            )

        def delete_system_models(self):
            system_name = self.system.currentText()
            self.refresh_systems(system_name)
            if self.system.currentText().casefold() != system_name.casefold():
                QMessageBox.information(self, "Game system changed", "Choose the Game System again before deleting models.")
                return
            system_id = self.system.currentData()
            if not system_id:
                QMessageBox.information(self, "No database system", "This Game System is not in the database.")
                return
            count = service.count_models_for_game_system(system_id)
            if count == 0:
                QMessageBox.information(self, "Nothing to delete", f"{system_name} has no physical models.")
                return
            answer = QMessageBox.warning(
                self,
                "Delete all models?",
                f"Delete all {count} physical models from {system_name}?\n\n"
                "Game System, Factions, Units, and rules files will be kept. "
                "This cannot be undone.",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                return
            try:
                deleted = service.delete_models_for_game_system(system_id)
            except CollectionError as exc:
                QMessageBox.warning(self, "Could not delete models", str(exc))
                return
            collection_page.refresh_all()
            QMessageBox.information(self, "Models deleted", f"Deleted {deleted} physical models from {system_name}.")

        def delete_game_system(self):
            system_name = self.system.currentText()
            system_id = self.system.currentData()
            if not system_id:
                QMessageBox.information(self, "No game system", "Choose a Game System to delete.")
                return
            count = service.count_models_for_game_system(system_id)
            answer = QMessageBox.warning(
                self,
                "Delete Game System and all related files?",
                f"Delete {system_name} and EVERYTHING exclusively bound to it?\n\n"
                f"Database: all Factions, Units, {count} Physical Models, Configurations and weapons.\n"
                "Files: Import Parser Rule JSON, Official Unit Data JSON, Change Log, saved URL, "
                "and the Game System rule directory.\n\nThis cannot be undone.",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                return
            typed, ok = QInputDialog.getText(
                self, "Final deletion confirmation",
                f"Type the exact Game System name to delete:\n{system_name}"
            )
            if not ok or typed != system_name:
                QMessageBox.information(self, "Deletion cancelled", "The Game System name did not match.")
                return
            try:
                service.delete_game_system(system_id)
                rules.remove_game_system(system_name, system_id)
            except (CollectionError, OSError) as exc:
                QMessageBox.warning(self, "Could not delete game system", str(exc))
                return
            refresh_game_system_views()
            QMessageBox.information(self, "Game system deleted", f"Deleted {system_name} from WMS.")

    class DashboardPage(QWidget):
        class StatCard(QFrame):
            def __init__(self, caption, accent):
                super().__init__()
                self.accent = accent
                self.setObjectName("dashboardCard")
                self.setMinimumHeight(104)
                card_layout = QVBoxLayout(self)
                card_layout.setContentsMargins(18, 14, 18, 14)
                self.value = QLabel("0")
                self.value.setStyleSheet(
                    "font-size: 30px; font-weight: 700; color: #ffffff; background: transparent;"
                )
                label = QLabel(caption.upper())
                label.setStyleSheet(
                    "font-size: 11px; font-weight: 600; letter-spacing: 1px; "
                    "color: #ffffff; background: transparent;"
                )
                card_layout.addWidget(self.value)
                card_layout.addWidget(label)

            def set_value(self, value):
                self.value.setText(f"{value:,}")

        class CollectionChart(QWidget):
            COLORS = ("#d6a84b", "#8f6ed5", "#4fa3c8", "#62b889", "#d06b69", "#ca7eb8")

            def __init__(self):
                super().__init__()
                self.items = []
                self.chart_type = "bar"
                self.setMinimumHeight(210)
                self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

            def set_items(self, items, chart_type="bar"):
                self.items = list(items)
                self.chart_type = chart_type
                self.update()

            def paintEvent(self, event):
                painter = QPainter(self)
                painter.setRenderHint(QPainter.Antialiasing)
                if not self.items:
                    painter.setPen(QColor("#ffffff"))
                    painter.drawText(self.rect(), Qt.AlignCenter, "No collection data yet")
                    return
                if self.chart_type == "pie":
                    self._paint_pie(painter)
                    return
                left, right, top, bottom = 150, 62, 16, 16
                width = max(20, self.width() - left - right)
                row_height = max(27, (self.height() - top - bottom) // len(self.items))
                maximum = max(item.value for item in self.items) or 1
                for index, item in enumerate(self.items):
                    y = top + index * row_height
                    label = item.label if len(item.label) <= 24 else item.label[:22] + "…"
                    painter.setPen(QColor("#ffffff"))
                    painter.drawText(QRectF(0, y, left - 12, row_height - 7), Qt.AlignRight | Qt.AlignVCenter, label)
                    track = QRectF(left, y + 5, width, max(10, row_height - 17))
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(QColor("#252c38"))
                    painter.drawRoundedRect(track, 5, 5)
                    bar = QRectF(track.x(), track.y(), max(5, track.width() * item.value / maximum), track.height())
                    painter.setBrush(QColor(self.COLORS[index % len(self.COLORS)]))
                    painter.drawRoundedRect(bar, 5, 5)
                    painter.setPen(QColor("#ffffff"))
                    painter.drawText(QRectF(left + width + 8, y, right - 8, row_height - 7), Qt.AlignLeft | Qt.AlignVCenter, f"{item.value:,}")

            def _paint_pie(self, painter):
                total = sum(max(0, item.value) for item in self.items)
                if total <= 0:
                    painter.setPen(QColor("#ffffff"))
                    painter.drawText(self.rect(), Qt.AlignCenter, "No positive values to chart")
                    return
                diameter = min(self.height() - 28, max(120, self.width() // 2))
                pie = QRectF(20, (self.height() - diameter) / 2, diameter, diameter)
                start = 90 * 16
                legend_x = pie.right() + 28
                legend_width = max(120, self.width() - int(legend_x) - 12)
                row_height = max(19, min(28, (self.height() - 16) // max(1, len(self.items))))
                for index, item in enumerate(self.items):
                    span = -round(360 * 16 * item.value / total)
                    color = QColor(self.COLORS[index % len(self.COLORS)])
                    painter.setPen(QPen(QColor("#171c24"), 1))
                    painter.setBrush(color)
                    painter.drawPie(pie, start, span)
                    start += span
                    y = 8 + index * row_height
                    painter.setPen(Qt.NoPen)
                    painter.drawRect(QRectF(legend_x, y + 4, 12, 12))
                    painter.setPen(QColor("#ffffff"))
                    percent = item.value / total * 100
                    label = f"{item.label}: {item.value:,} ({percent:.1f}%)"
                    painter.drawText(
                        QRectF(legend_x + 19, y, legend_width - 19, row_height),
                        Qt.AlignLeft | Qt.AlignVCenter, label,
                    )

        def __init__(self):
            super().__init__()
            self.setObjectName("dashboardPage")
            layout = QVBoxLayout(self)
            layout.setContentsMargins(26, 22, 26, 22)
            layout.setSpacing(16)
            heading = QHBoxLayout()
            title_box = QVBoxLayout()
            title = QLabel("COMMAND DASHBOARD")
            title.setStyleSheet("font-size: 22px; font-weight: 700; color: #ffffff;")
            subtitle = QLabel("Collection strength by game system and faction")
            subtitle.setStyleSheet("color: #ffffff;")
            title_box.addWidget(title)
            title_box.addWidget(subtitle)
            heading.addLayout(title_box)
            heading.addStretch()
            self.system_filter = QComboBox()
            self.system_filter.setMinimumWidth(190)
            self.system_filter.currentTextChanged.connect(self.refresh)
            refresh = QPushButton("↻  Refresh")
            refresh.clicked.connect(self.refresh)
            heading.addWidget(self.system_filter)
            heading.addWidget(refresh)
            layout.addLayout(heading)

            chart_controls = QHBoxLayout()
            self.metric_filter = QComboBox()
            self.metric_filter.addItem("Faction total points", "faction_points")
            self.metric_filter.addItem("Game System total points", "system_points")
            self.metric_filter.addItem("Game System model count", "system_models")
            self.metric_filter.addItem("Faction assembled / not assembled", "faction_assembly")
            self.metric_filter.addItem("Faction painted / not painted", "faction_paint")
            self.chart_type = QComboBox()
            self.chart_type.addItem("Bar chart", "bar")
            self.chart_type.addItem("Pie chart", "pie")
            self.metric_filter.currentIndexChanged.connect(self.refresh_chart)
            self.chart_type.currentIndexChanged.connect(self.refresh_chart)
            chart_controls.addWidget(QLabel("Display"))
            chart_controls.addWidget(self.metric_filter, 1)
            chart_controls.addWidget(QLabel("Chart"))
            chart_controls.addWidget(self.chart_type)
            layout.addLayout(chart_controls)

            cards = QHBoxLayout()
            cards.setSpacing(14)
            self.points_card = self.StatCard("Total points", "#d6a84b")
            self.units_card = self.StatCard("Unit instances", "#8f6ed5")
            self.models_card = self.StatCard("Physical models", "#4fa3c8")
            cards.addWidget(self.points_card)
            cards.addWidget(self.units_card)
            cards.addWidget(self.models_card)
            layout.addLayout(cards)

            chart_frame = QFrame()
            chart_frame.setObjectName("dashboardPanel")
            chart_layout = QVBoxLayout(chart_frame)
            self.chart_title = QLabel("FACTION TOTAL POINTS")
            self.chart_title.setStyleSheet("font-size: 12px; font-weight: 700; color: #ffffff;")
            self.chart = self.CollectionChart()
            chart_layout.addWidget(self.chart_title)
            chart_layout.addWidget(self.chart)
            layout.addWidget(chart_frame, 1)

            self.table = QTableWidget(0, 5)
            self.table.setHorizontalHeaderLabels(
                ("Game System", "Faction", "Unit Instances", "Physical Models", "Total Points")
            )
            self.table.setEditTriggers(QTableWidget.NoEditTriggers)
            self.table.setAlternatingRowColors(True)
            self.table.setSortingEnabled(True)
            self.table.verticalHeader().setVisible(False)
            self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            self.table.setMinimumHeight(180)
            layout.addWidget(self.table)

        def refresh(self, *_):
            self.dashboard_rows = service.list_collection()
            totals = _dashboard_totals(self.dashboard_rows, rules)
            self.dashboard_totals = totals
            systems = ["All Game Systems"] + sorted({item.game_system for item in totals}, key=_natural_key)
            selected = self.system_filter.currentText() or "All Game Systems"
            self.system_filter.blockSignals(True)
            self.system_filter.clear()
            self.system_filter.addItems(systems)
            self.system_filter.setCurrentText(selected if selected in systems else "All Game Systems")
            self.system_filter.blockSignals(False)
            selected = self.system_filter.currentText()
            visible = totals if selected == "All Game Systems" else [
                item for item in totals if item.game_system == selected
            ]
            self.table.setSortingEnabled(False)
            self.table.setRowCount(len(visible))
            for row_number, total in enumerate(visible):
                values = (
                    total.game_system, total.faction, str(total.unit_instances),
                    str(total.physical_models), str(total.points),
                )
                for column, value in enumerate(values):
                    self.table.setItem(row_number, column, QTableWidgetItem(value))
            self.table.setSortingEnabled(True)
            self.points_card.set_value(sum(item.points for item in visible))
            self.units_card.set_value(sum(item.unit_instances for item in visible))
            self.models_card.set_value(sum(item.physical_models for item in visible))
            self.refresh_chart()

        def refresh_chart(self, *_):
            if not hasattr(self, "dashboard_totals"):
                return
            selected = self.system_filter.currentText() or "All Game Systems"
            metric = self.metric_filter.currentData() or "faction_points"
            labels = {
                "faction_points": "FACTION TOTAL POINTS",
                "system_points": "GAME SYSTEM TOTAL POINTS",
                "system_models": "GAME SYSTEM PHYSICAL MODEL COUNT",
                "faction_assembly": "FACTION ASSEMBLED / NOT ASSEMBLED",
                "faction_paint": "FACTION PAINTED / NOT PAINTED",
            }
            self.chart_title.setText(labels[metric])
            values = _dashboard_chart_values(
                self.dashboard_rows, self.dashboard_totals, metric, selected
            )
            self.chart.set_items(values, self.chart_type.currentData() or "bar")

    class ExportPage(QWidget):
        def __init__(self):
            super().__init__()
            layout = QVBoxLayout(self)
            title = QLabel("Export Faction Data")
            title.setStyleSheet("font-size: 18px; font-weight: 600;")
            layout.addWidget(title)
            layout.addWidget(QLabel(
                "Export a complete SQLite database or an Excel-friendly CSV snapshot for one Faction."
            ))
            form = QFormLayout()
            self.system = QComboBox()
            self.faction = QComboBox()
            self.format = QComboBox()
            self.format.addItem("SQLite database — complete data", "sqlite")
            self.format.addItem("CSV — current collection view", "csv")
            form.addRow("Game System", self.system)
            form.addRow("Faction", self.faction)
            form.addRow("Format", self.format)
            layout.addLayout(form)
            button = QPushButton("Export Selected Faction…")
            button.clicked.connect(self.export_selected)
            layout.addWidget(button)
            layout.addStretch(1)
            self.system.currentIndexChanged.connect(self.refresh_factions)
            self.refresh()

        def refresh(self):
            selected = self.system.currentData()
            self.system.blockSignals(True)
            self.system.clear()
            for system_id, name in service.list_named("game_system"):
                self.system.addItem(name, system_id)
            index = self.system.findData(selected)
            self.system.setCurrentIndex(index if index >= 0 else (0 if self.system.count() else -1))
            self.system.blockSignals(False)
            self.refresh_factions()

        def refresh_factions(self):
            selected = self.faction.currentData()
            self.faction.clear()
            system_id = self.system.currentData()
            if system_id:
                for faction_id, name in service.list_named("faction", "game_system_id", system_id):
                    self.faction.addItem(name, faction_id)
            index = self.faction.findData(selected)
            if index >= 0:
                self.faction.setCurrentIndex(index)

        def export_selected(self):
            system, faction = self.system.currentText(), self.faction.currentText()
            if not system or not faction:
                QMessageBox.information(self, "Nothing to export", "Choose a Game System and Faction.")
                return
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", faction).strip("_") or "Faction"
            export_format = self.format.currentData()
            extension = "csv" if export_format == "csv" else "sqlite3"
            file_filter = "CSV files (*.csv)" if export_format == "csv" else "SQLite database (*.sqlite3)"
            filename, _ = QFileDialog.getSaveFileName(
                self, "Export Faction data", f"WMS_{safe_name}.{extension}", file_filter,
            )
            if not filename:
                return
            try:
                if export_format == "csv":
                    units, models = _export_faction_csv(service, rules, system, faction, filename)
                else:
                    units, models = service.export_faction_database(system, faction, filename)
            except (CollectionError, OSError, csv.Error, sqlite3.Error) as exc:
                QMessageBox.warning(self, "Export failed", str(exc))
                return
            QMessageBox.information(
                self, "Export complete",
                f"Exported {faction}: {units} Unit instances and {models} Physical models."
            )

    app = QApplication.instance() or QApplication([])
    combo_arrow_path = (Path(__file__).with_name("assets") / "combo_down_arrow.svg").as_posix()
    app.setStyleSheet("""
        QMainWindow, QDialog, QMessageBox, QInputDialog, QWidget#dashboardPage {
            background: #11151c; color: #ffffff;
        }
        QLabel, QCheckBox, QRadioButton, QGroupBox { color: #ffffff; }
        QLabel:disabled, QCheckBox:disabled, QRadioButton:disabled,
        QGroupBox:disabled { color: #aab2bf; }
        QListWidget { background: #171c25; color: #ffffff; border: 0; padding: 10px 6px; }
        QListWidget#primaryNavigation { padding: 12px 8px; }
        QListWidget#primaryNavigation::item {
            height: 48px; padding: 0 14px; margin: 4px 0; color: #ffffff;
            border-radius: 6px;
        }
        QListWidget#primaryNavigation::item:selected {
            background: #6f54a3; color: #ffffff; font-weight: 700;
        }
        QListWidget::item:selected, QTableWidget::item:selected, QTreeWidget::item:selected {
            background: #6f54a3; color: #ffffff;
        }
        QFrame#dashboardCard, QFrame#dashboardPanel {
            background: #1a202a; border: 1px solid #2b3340; border-radius: 10px;
        }
        QTableWidget {
            background: #181e27; alternate-background-color: #1d242e; color: #ffffff;
            border: 1px solid #2b3340; border-radius: 8px; gridline-color: #2b3340;
            selection-background-color: #6f54a3; selection-color: #ffffff;
        }
        QTreeWidget {
            background: #181e27; alternate-background-color: #1d242e; color: #ffffff;
            border: 1px solid #2b3340; border-radius: 8px;
            selection-background-color: #4b4263; selection-color: #ffffff;
        }
        QTreeWidget::item:selected { background: #6f54a3; color: #ffffff; }
        QHeaderView::section {
            background: #252c37; color: #ffffff; border: 0; border-right: 1px solid #333c49;
            padding: 8px; font-weight: 600;
        }
        QPushButton {
            background: #393247; color: #ffffff; border: 1px solid #554865;
            border-radius: 6px; padding: 7px 13px; font-weight: 600;
        }
        QPushButton:hover { background: #493f59; }
        QPushButton:disabled { background: #252a32; color: #aab2bf; border-color: #343b46; }
        QComboBox, QLineEdit, QSpinBox, QTextEdit, QListWidget, QTableWidget, QTreeWidget {
            background: #273140; color: #ffffff; border: 1px solid #617087;
            border-radius: 5px; padding: 6px;
        }
        QLineEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus,
        QListWidget:focus, QTableWidget:focus, QTreeWidget:focus {
            background: #2d394a; border: 2px solid #9f83cf;
        }
        QLineEdit:read-only, QTextEdit:read-only {
            background: #171c24; color: #c0c7d2; border: 1px solid #303946;
        }
        QComboBox QAbstractItemView {
            background: #171c25; color: #ffffff; border: 1px solid #394250;
            selection-background-color: #6f54a3; selection-color: #ffffff;
            outline: 0; padding: 4px;
        }
        QComboBox:disabled, QLineEdit:disabled, QSpinBox:disabled, QTextEdit:disabled {
            background: #1a1f28; color: #aab2bf; border-color: #303744;
        }
        QMenu {
            background: #171c25; color: #ffffff; border: 1px solid #394250;
        }
        QMenu::item:selected { background: #6f54a3; color: #ffffff; }
        QRadioButton, QCheckBox { spacing: 8px; padding: 4px; }
        QRadioButton::indicator, QCheckBox::indicator {
            width: 17px; height: 17px; border: 2px solid #c9d1dd; background: #273140;
        }
        QRadioButton::indicator { border-radius: 10px; }
        QCheckBox::indicator { border-radius: 3px; }
        QRadioButton::indicator:hover, QCheckBox::indicator:hover {
            border-color: #c8a9ff; background: #354156;
        }
        QRadioButton::indicator:checked, QCheckBox::indicator:checked {
            background: #9f83cf; border: 4px solid #e8ddff;
        }
        QRadioButton::indicator:disabled, QCheckBox::indicator:disabled {
            background: #202630; border-color: #687282;
        }
        QTabWidget::pane { border: 1px solid #617087; background: #171c24; }
        QTabBar::tab {
            background: #273140; color: #dce2eb; border: 2px solid #4b586b;
            border-bottom-color: #617087; padding: 9px 18px; margin-right: 3px;
        }
        QTabBar::tab:hover {
            background: #354156; border: 2px solid #6f7f96;
        }
        QTabBar::tab:selected, QTabBar::tab:selected:hover {
            background: #6f54a3; color: #ffffff; border: 2px solid #c8a9ff; font-weight: 700;
        }
        QComboBox::drop-down { border: 0; width: 30px; }
        QComboBox::down-arrow {
            image: url(__COMBO_ARROW_PATH__); width: 14px; height: 10px;
        }
        QToolTip { background: #252c37; color: #ffffff; border: 1px solid #46505f; }
    """.replace("__COMBO_ARROW_PATH__", combo_arrow_path))
    window = QMainWindow()
    window.setWindowTitle("Warhammer Management System — Revision 1.0.10")
    window.resize(1100, 720)
    root = QWidget()
    layout = QHBoxLayout(root)
    navigation = QListWidget()
    navigation.setObjectName("primaryNavigation")
    navigation.setSpacing(4)
    navigation.setMinimumWidth(150)
    pages = QStackedWidget()
    collection_page = CollectionPage()
    import_page = ImportPage(collection_page)
    settings_page = SettingsPage()
    dashboard_page = DashboardPage()
    export_page = ExportPage()
    def refresh_game_system_views(selected_id=None, selected_name=None):
        import_page.refresh_systems(selected_id)
        settings_page.refresh_systems(selected_name)
        collection_page.refresh_all()
    for name in ("Dashboard", "Collection", "Import", "Export", "Settings"):
        navigation.addItem(name)
        if name == "Collection":
            page = collection_page
        elif name == "Import":
            page = import_page
        elif name == "Settings":
            page = settings_page
        elif name == "Dashboard":
            page = dashboard_page
        elif name == "Export":
            page = export_page
        else:
            page = QLabel(f"{name}\n\nThis page is part of the v0.1 implementation roadmap.")
            page.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        pages.addWidget(page)
    def change_page(index):
        pages.setCurrentIndex(index)
        if index in (1, 2, 4):
            refresh_game_system_views()
        if index == 0:
            dashboard_page.refresh()
        elif index == 3:
            export_page.refresh()

    navigation.currentRowChanged.connect(change_page)
    navigation.setCurrentRow(0)
    splitter = QSplitter(Qt.Horizontal)
    splitter.addWidget(navigation)
    splitter.addWidget(pages)
    splitter.setStretchFactor(0, 0)
    splitter.setStretchFactor(1, 1)
    splitter.setSizes([190, 910])
    splitter.setChildrenCollapsible(False)
    layout.addWidget(splitter)
    window.setCentralWidget(root)
    window.show()
    return app.exec()
