from __future__ import annotations

import json
import csv
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from wms.collection import CollectionError, CollectionService
from wms.db import MIGRATIONS, Database
from wms.gw_import import (
    GWImportError, ImportedPhysicalModel, ImportedUnit, ParsedArmy, parse_gw_army_text,
)
from wms.gui import (
    _collection_model_name, _collection_points, _collection_points_mismatch,
    _collection_weapon, _dashboard_totals,
    _display_points, _natural_key,
    _collection_filter_values, _export_faction_csv, _import_wms_collection_csv, _profiles_for_faction,
    _resolve_unit_instance_points, _dashboard_chart_values, _points_filter_matches,
    _target_unit_size, _missing_unit_points,
)
import wms.rules as rules_module
from wms.rules import RulesError, RulesManager, UnitProfile


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "wms.sqlite3")
    db.initialize()
    return db


@pytest.fixture
def collection(database):
    return CollectionService(database)


def build_hierarchy(collection):
    system = collection.create_game_system("Age of Sigmar")
    faction = collection.create_faction(system, "Helsmiths of Hashut")
    unit = collection.create_unit(faction, "Bull Centaurs")
    model = collection.create_model(unit, display_name="Bull Centaur 1")
    return system, faction, unit, model


def test_initialization_is_idempotent_and_enables_foreign_keys(database):
    database.initialize()
    assert database.schema_version() == 8
    connection = database.connect()
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        connection.close()


def test_version_two_database_upgrades_without_losing_existing_configuration(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    legacy = Database(path)
    with legacy.connect() as connection:
        connection.executescript(MIGRATIONS[0])
        connection.executescript(MIGRATIONS[1])
        connection.execute("PRAGMA user_version = 2")
    timestamp = "2026-08-03T00:00:00+00:00"
    with legacy.connect() as connection:
        connection.execute("INSERT INTO game_system VALUES ('system', 'Age of Sigmar', ?, ?)", (timestamp, timestamp))
        connection.execute("INSERT INTO faction VALUES ('faction', 'system', 'Helsmiths of Hashut', ?, ?)", (timestamp, timestamp))
        connection.execute("INSERT INTO unit VALUES ('unit', 'faction', 'Bull Centaurs', ?, ?)", (timestamp, timestamp))
        connection.execute(
            """INSERT INTO physical_model VALUES
               ('model', 'unit', 'Bull Centaur 1', 'unassembled', 'unpainted', 0, NULL, NULL, ?, ?)""",
            (timestamp, timestamp),
        )
        connection.execute(
            """INSERT INTO configuration
               (id, physical_model_id, name, is_active, notes, created_at, updated_at)
               VALUES ('legacy-config', ?, 'Legacy state', 1, 'Keep me', ?, ?)""",
            ("model", timestamp, timestamp),
        )
    legacy.initialize()
    service = CollectionService(legacy)
    configuration = service.list_configurations("model")[0]
    assert (legacy.schema_version(), configuration.name, configuration.notes) == (
            8, "Legacy state", "Keep me"
    )


def test_version_three_configuration_is_classified_during_upgrade(tmp_path):
    path = tmp_path / "v3.sqlite3"
    legacy = Database(path)
    with legacy.connect() as connection:
        for script in MIGRATIONS[:3]:
            connection.executescript(script)
        connection.execute("PRAGMA user_version = 3")
    timestamp = "2026-08-03T00:00:00+00:00"
    with legacy.connect() as connection:
        connection.execute("INSERT INTO game_system VALUES ('system', 'Age of Sigmar', ?, ?)", (timestamp, timestamp))
        connection.execute("INSERT INTO faction VALUES ('faction', 'system', 'Helsmiths of Hashut', ?, ?)", (timestamp, timestamp))
        connection.execute("INSERT INTO unit VALUES ('unit', 'faction', 'Bull Centaurs', ?, ?)", (timestamp, timestamp))
        connection.execute(
            """INSERT INTO physical_model VALUES
               ('model', 'unit', 'Bull Centaur 1', 'unassembled', 'unpainted', 0, NULL, NULL, ?, ?)""",
            (timestamp, timestamp),
        )
        connection.execute(
            """INSERT INTO configuration
               (id, physical_model_id, name, represented_unit_id, loadout_name,
                points, is_active, notes, created_at, updated_at)
               VALUES ('legacy-model', ?, 'Legacy model', ?, NULL, 150, 1, NULL, ?, ?)""",
            ("model", "unit", timestamp, timestamp),
        )
    legacy.initialize()
    service = CollectionService(legacy)
    configuration = service.list_configurations("model")[0]
    assert (
        legacy.schema_version(), configuration.configuration_type,
        configuration.rule_faction, configuration.rule_model_name,
    ) == (8, "model", "Helsmiths of Hashut", "Bull Centaurs")


def test_complete_collection_hierarchy(collection):
    _, _, _, model_id = build_hierarchy(collection)
    configuration_id = collection.create_configuration(
        model_id, "Hashutite Weapons", configuration_type="weapon",
        loadout_name="Hashutite Weapons",
    )

    models = collection.list_models()
    assert len(models) == 1
    assert models[0].display_name == "Bull Centaur 1"
    assert configuration_id


def test_units_and_models_have_readable_unique_codes(collection):
    _, faction, unit_id, model_id = build_hierarchy(collection)
    second_unit = collection.create_unit(faction, "Bull Centaurs")
    second_model = collection.create_model(second_unit, display_name="Bull Centaur 1")
    rows = collection.list_collection()
    assert all(row.unit_code.startswith("U-AOS-BULL-CENTAURS-") for row in rows)
    assert all(row.model_code.startswith("M-AOS-BULL-CENTAURS-") for row in rows)
    assert len({row.unit_code for row in rows}) == 2
    assert len({row.model_code for row in rows}) == 2
    assert {row.id for row in rows} == {model_id, second_model}


def test_collection_names_use_natural_number_sorting():
    names = ["Model 10", "Model 2", "Model 1"]
    assert sorted(names, key=_natural_key) == ["Model 1", "Model 2", "Model 10"]


def test_magnetized_model_profiles_are_limited_to_current_faction_and_naturally_sorted():
    profiles = [
        {"faction": "Helsmiths of Hashut", "name": "Ashen Elder 10", "points": 120},
        {"faction": "Orruk Warclans", "name": "Ashen Elder 1", "points": 100},
        {"faction": "helsmiths OF hashut", "name": "Ashen Elder 2", "points": 110},
        {"faction": "Helsmiths of Hashut", "name": "Ashen Elder 1", "points": 100},
    ]

    choices = _profiles_for_faction(profiles, "Helsmiths of Hashut")

    assert [choice["name"] for choice in choices] == [
        "Ashen Elder 1", "Ashen Elder 2", "Ashen Elder 10",
    ]


def test_points_load_from_pdf_rule_when_magnetized_is_off(tmp_path, collection):
    _, _, _, model_id = build_hierarchy(collection)
    manager = RulesManager(tmp_path / "rules.json")
    manager.ensure_game_system("Age of Sigmar")
    manager.pdf_rule_path("Age of Sigmar").write_text(json.dumps({
        "game_system": "Age of Sigmar",
        "profiles": [{
            "faction": "Helsmiths of Hashut", "name": "Bull Centaurs",
            "unit_size": 3, "points": 180,
        }],
    }), encoding="utf-8")

    row = collection.list_collection()[0]
    assert row.id == model_id
    assert row.is_magnetized is False
    assert row.current_points is None
    assert _collection_points(row, manager) is None
    assert _collection_points_mismatch(row, manager) is True


def test_magnetized_points_are_resolved_from_active_unit_data_not_database_cache(tmp_path, collection):
    _, _, _, model_id = build_hierarchy(collection)
    collection.update_model(
        model_id, display_name="Bull Centaur 1", assembly_status="unassembled",
        paint_status="unpainted", is_magnetized=True, storage_location=None, notes=None,
    )
    collection.create_configuration(
        model_id, "Ashen Elder", configuration_type="model",
        rule_faction="Helsmiths of Hashut", rule_model_name="Ashen Elder", points=130,
    )
    manager = RulesManager(tmp_path / "rules.json")
    manager.ensure_game_system("Age of Sigmar")
    manager.pdf_rule_path("Age of Sigmar").write_text(json.dumps({
        "game_system": "Age of Sigmar",
        "profiles": [
            {
                "faction": "Helsmiths of Hashut", "name": "Bull Centaurs",
                "unit_size": 3, "points": 180,
            },
            {
                "faction": "Helsmiths of Hashut", "name": "Ashen Elder",
                "unit_size": 1, "points": 120,
            },
        ],
    }), encoding="utf-8")

    row = collection.list_collection()[0]
    # Configuration cache and Unit Data do not silently replace database Points.
    assert _collection_points(row, manager) is None
    assert _collection_points_mismatch(row, manager) is True


def test_active_model_event_writes_standard_points_to_unit_database(collection):
    _, _, unit_id, model_id = build_hierarchy(collection)
    configuration_id = collection.create_configuration(
        model_id, "Ashen Elder", configuration_type="model",
        rule_faction="Helsmiths of Hashut", rule_model_name="Ashen Elder",
        points=999, is_active=False,
    )

    collection.set_active_configuration(model_id, configuration_id, unit_points=120)
    row = collection.list_collection()[0]
    assert (row.represented_unit, row.unit_points, row.unit_points_manual) == (
        "Ashen Elder", 120, False,
    )

    collection.clear_active_configuration(model_id, "model", unit_points=180)
    row = collection.list_collection()[0]
    assert (row.represented_unit, row.unit_points, row.unit_points_manual) == (
        None, 180, False,
    )


def test_manual_database_points_are_displayed_and_flagged_against_unit_data(tmp_path, collection):
    _, _, unit_id, _ = build_hierarchy(collection)
    manager = RulesManager(tmp_path)
    manager.ensure_game_system("Age of Sigmar")
    manager.pdf_rule_path("Age of Sigmar").write_text(json.dumps({
        "game_system": "Age of Sigmar",
        "profiles": [{
            "faction": "Helsmiths of Hashut", "name": "Bull Centaurs",
            "unit_size": 3, "points": 180,
        }],
    }), encoding="utf-8")

    collection.set_unit_points(unit_id, 175, manual=True)
    row = collection.list_collection()[0]
    assert _collection_points(row, manager) == 175
    assert _collection_points_mismatch(row, manager) is True

    collection.set_unit_points(unit_id, 180, manual=True)
    assert _collection_points_mismatch(collection.list_collection()[0], manager) is False


def test_collection_name_follows_active_alternative_model(collection):
    _, _, _, model_id = build_hierarchy(collection)
    collection.update_model(
        model_id, display_name="Bull Centaur 1", assembly_status="unassembled",
        paint_status="unpainted", is_magnetized=True, storage_location=None, notes=None,
    )
    collection.create_configuration(
        model_id, "Internal configuration label", configuration_type="model",
        rule_faction="Helsmiths of Hashut", rule_model_name="Ashen Elder", points=130,
    )

    row = collection.list_collection()[0]
    assert row.current_model_configuration == "Internal configuration label"
    assert _collection_model_name(row) == "Ashen Elder"

    collection.clear_active_configuration(model_id, "model")
    assert _collection_model_name(collection.list_collection()[0]) == "Bull Centaurs"


def test_aos_units_display_default_weapon_without_a_weapon_configuration(collection):
    build_hierarchy(collection)
    row = collection.list_collection()[0]

    assert row.current_loadout is None
    assert _collection_weapon(row) == "Default"


def test_magnetized_model_switches_between_model_and_weapon_configurations(collection):
    _, _, _, model_id = build_hierarchy(collection)
    sword = collection.create_configuration(
        model_id, "Sword state", configuration_type="weapon",
        loadout_name="Darkforged Weapon", points=150,
    )
    axes = collection.create_configuration(
        model_id, "Renders state", configuration_type="model",
        rule_faction="Helsmiths of Hashut", rule_model_name="Bull Centaur Renders",
        points=170,
    )

    configurations = collection.list_configurations(model_id)
    assert [item.is_active for item in configurations] == [True, True]
    row = collection.list_collection()[0]
    assert (row.current_model_configuration, row.current_weapon_configuration,
            row.represented_unit, row.current_loadout, row.current_points) == (
        "Renders state", "Sword state", "Bull Centaur Renders", "Darkforged Weapon", 170,
    )

    collection.clear_active_configuration(model_id, "model")
    row = collection.list_collection()[0]
    assert row.current_model_configuration is None
    assert row.current_weapon_configuration == "Sword state"
    collection.clear_active_configuration(model_id, "weapon")
    assert collection.list_collection()[0].current_weapon_configuration is None


def test_configuration_type_cannot_mix_model_and_weapon(collection):
    _, _, _, model_id = build_hierarchy(collection)
    with pytest.raises(CollectionError, match="cannot also contain"):
        collection.create_configuration(
            model_id, "Invalid", configuration_type="model",
            rule_model_name="Bull Centaur Renders", loadout_name="Paired Axes",
        )


def test_model_configuration_requires_json_profile_name(collection):
    _, _, _, model_id = build_hierarchy(collection)
    with pytest.raises(CollectionError, match="rule file"):
        collection.create_configuration(
            model_id, "Missing model", configuration_type="model"
        )


def test_custom_model_name_is_allowed_when_profile_is_missing(collection):
    _, _, _, model_id = build_hierarchy(collection)
    configuration_id = collection.create_configuration(
        model_id, "Custom state", configuration_type="model",
        rule_faction="Helsmiths of Hashut", rule_model_name="Unlisted Prototype",
    )
    configuration = collection.list_configurations(model_id)[0]
    assert (configuration.id, configuration.rule_model_name) == (
        configuration_id, "Unlisted Prototype"
    )


def test_duplicate_name_is_rejected_within_parent(collection):
    system = collection.create_game_system("Age of Sigmar")
    collection.create_faction(system, "Helsmiths of Hashut")
    with pytest.raises(CollectionError):
        collection.create_faction(system, "helsmiths of hashut")


@pytest.mark.parametrize("name", ["Age of Sigma", "AoS", "Age of Sigmar", "Middle-earth Strategy Battle Game"])
def test_any_nonempty_game_system_name_can_be_created(collection, name):
    entity_id = collection.create_game_system(name)
    assert (entity_id, name) in collection.list_named("game_system")


def test_deleting_game_system_cascades_its_hierarchy(collection):
    system, _, _, _ = build_hierarchy(collection)
    collection.delete_game_system(system)
    assert collection.list_named("game_system") == []
    assert collection.list_collection() == []


def test_model_status_update_is_validated(collection):
    _, _, _, model_id = build_hierarchy(collection)
    collection.update_model_status(
        model_id, assembly_status="assembled", paint_status="in_progress"
    )
    model = collection.list_models()[0]
    assert model.assembly_status == "assembled"
    assert model.paint_status == "in_progress"

    with pytest.raises(CollectionError):
        collection.update_model_status(
            model_id, assembly_status="assembled", paint_status="excellent"
        )


def test_parent_delete_cascades_in_one_transaction(collection, database):
    system_id, _, _, model_id = build_hierarchy(collection)
    collection.create_configuration(
        model_id, "Default", configuration_type="weapon", loadout_name="Default"
    )
    collection.delete_game_system(system_id)

    with database.connect() as connection:
        for table in ("faction", "unit", "physical_model", "configuration"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_invalid_parent_is_reported_as_domain_error(collection):
    with pytest.raises(CollectionError):
        collection.create_faction("missing", "Unknown")


def test_collection_rows_include_hierarchy_and_can_be_edited(collection):
    _, _, _, model_id = build_hierarchy(collection)
    row = collection.list_collection()[0]
    assert (row.game_system, row.faction, row.unit) == (
        "Age of Sigmar", "Helsmiths of Hashut", "Bull Centaurs"
    )

    collection.update_model(
        model_id,
        display_name="Champion",
        assembly_status="assembled",
        paint_status="painted",
        is_magnetized=True,
        storage_location="Case A",
        notes="Finished",
    )
    updated = collection.list_collection()[0]
    assert updated.display_name == "Champion"
    assert updated.is_magnetized is True
    assert updated.storage_location == "Case A"


def test_hierarchy_choices_and_model_delete(collection):
    system_id, faction_id, unit_id, model_id = build_hierarchy(collection)
    assert collection.list_named("game_system") == [(system_id, "Age of Sigmar")]
    assert collection.list_named("faction", "game_system_id", system_id) == [
        (faction_id, "Helsmiths of Hashut")
    ]
    assert collection.list_named("unit", "faction_id", faction_id) == [
        (unit_id, "Bull Centaurs")
    ]
    collection.delete_model(model_id)
    assert collection.list_collection() == []


def test_collection_filters(collection):
    build_hierarchy(collection)
    assert len(collection.list_collection(faction="helsmiths of hashut")) == 1
    assert len(collection.list_collection(search="centaur")) == 1
    assert collection.list_collection(paint_status="painted") == []


def test_collection_filter_options_include_empty_systems_and_scope_factions(collection):
    aos = collection.create_game_system("Age of Sigmar")
    kharadron = collection.create_faction(aos, "Kharadron Overlords")
    unit = collection.create_unit(kharadron, "Arkanaut Company")
    collection.create_model(unit)
    collection.create_game_system("Middle-earth Strategy Battle Game")

    systems, factions, units = _collection_filter_values(collection)
    assert systems == {"Age of Sigmar", "Middle-earth Strategy Battle Game"}
    assert factions == {"Kharadron Overlords"}
    assert units == {"Arkanaut Company"}

    _, factions, units = _collection_filter_values(
        collection, "Middle-earth Strategy Battle Game"
    )
    assert factions == set()
    assert units == set()

    mesbg = next(
        entity_id for entity_id, name in collection.list_named("game_system")
        if name == "Middle-earth Strategy Battle Game"
    )
    collection.create_faction(mesbg, "Mordor")
    _, factions, units = _collection_filter_values(
        collection, "Middle-earth Strategy Battle Game"
    )
    assert factions == {"Mordor"}
    assert units == set()
    assert collection.list_collection(is_magnetized=True) == []


def test_parse_gw_export_counts_only_shallow_model_rows():
    parsed = parse_gw_army_text("""
+ FACTION KEYWORD: Imperium - Adeptus Astartes

Intercessor Squad (80 Points)
    • 1x Intercessor Sergeant
        • 1x Bolt rifle
    • 4x Intercessor
        • 4x Bolt pistol

Captain in Gravis Armour (80 Points)
    • 1x Master-crafted heavy bolt rifle
""")
    assert parsed.faction == "Imperium - Adeptus Astartes"
    assert parsed.units == (
        ImportedUnit("Intercessor Squad", 5),
        ImportedUnit("Captain in Gravis Armour", 1),
    )


def test_parse_gw_export_rejects_missing_metadata():
    with pytest.raises(GWImportError):
        parse_gw_army_text("Intercessor Squad (80 Points)")


def test_parse_aos_inventory_export_uses_line_after_inventory_title_as_faction():
    parsed = parse_gw_army_text("""
Chaos Dwarf Inventory 27680/3000 pts

Helsmiths of Hashut
Castigation Battery
General's Handbook 2026-27
Auxiliary Units: 47 (+21620 Points)
Drops: 47
Auxiliary Units
Anointed Sentinels (130)
Anointed Sentinels (130)
Ashen Elder (120)
Bull Centaurs (190)
Bull Centaurs (190)
Urak Taar, the First Daemonsmith (340)
War Despot (80)
""")

    assert parsed.faction == "Helsmiths of Hashut"
    assert parsed.game_system == "Age of Sigmar"
    assert parsed.units == (
        ImportedUnit("Anointed Sentinels", 1),
        ImportedUnit("Anointed Sentinels", 1),
        ImportedUnit("Ashen Elder", 1),
        ImportedUnit("Bull Centaurs", 1),
        ImportedUnit("Bull Centaurs", 1),
        ImportedUnit("Urak Taar, the First Daemonsmith", 1),
        ImportedUnit("War Despot", 1),
    )


def test_inventory_summary_is_not_mistaken_for_a_unit():
    parsed = parse_gw_army_text("""
Chaos Dwarf Inventory 27680/3000 pts
Helsmiths of Hashut
Auxiliary Units: 47 (+21620 Points)
Bull Centaurs (190)
""")

    assert parsed.units == (ImportedUnit("Bull Centaurs", 1),)


def test_faction_export_selects_40k_parser():
    parsed = parse_gw_army_text("""
+ FACTION KEYWORD: Imperium - Adeptus Astartes
Intercessor Squad (80 Points)
""")
    assert parsed.game_system == "Warhammer 40,000"


def test_current_40k_app_inventory_export_detects_faction_models_and_vehicle():
    parsed = parse_gw_army_text("""
Votan Inventory (7390 points)

Leagues of Votann
No Detachment
Onslaught (3000 points)

CHARACTERS

Brôkhyr Iron-master (70 points)
  • 1x Brôkhyr Iron-master
    • 1x Graviton hammer
      1x Graviton rifle
  • 1x Ironkin Assistant
    • 1x Close combat weapon
  • 1x E-COG
    • 1x Plasma torch

Hekaton Land Fortress (250 points)
  • 1x Armoured wheels
    1x Cyclic ion cannon
    2x Twin bolt cannon

Exported with App Version: v2.3.1 (138), Data Version: v913
""")
    assert parsed.game_system == "Warhammer 40,000"
    assert parsed.faction == "Leagues of Votann"
    assert parsed.units == (
        ImportedUnit("Brôkhyr Iron-master", 3),
        ImportedUnit("Hekaton Land Fortress", 1),
    )


def test_40k_rule_generation_is_system_scoped_and_faction_is_detected(tmp_path):
    manager = RulesManager(tmp_path)
    sample = """
Votan Inventory (180 points)
Leagues of Votann
No Detachment
Sagitaur (85 points)
  • 1x Armoured wheels
    1x HYLas beam cannon
Sagitaur (95 points)
  • 1x Armoured wheels
    1x HYLas beam cannon
Exported with App Version: v2.3.1 (138), Data Version: v913
"""
    assert manager.generate_from_text("Warhammer 40,000", sample) == 2
    payload = json.loads(manager.app_rule_path("Warhammer 40,000").read_text(encoding="utf-8"))
    assert payload["game_system"] == "Warhammer 40,000"
    assert payload["parser"]["type"] == "declarative"
    assert payload["parser"]["dialect"] == "gw_app_40k"
    assert payload["parser"]["logic"]["weapon_assignment"] == "nested_under_model"
    assert "profiles" not in payload
    assert payload["sample"]["unit_entries"] == 2
    assert payload["sample"]["detected_faction"] == "Leagues of Votann"
    assert manager.unit_size("Leagues of Votann", "Sagitaur", "Warhammer 40,000") is None
    assert manager.parser_type("Age of Sigmar") == ""


def test_app_and_pdf_parser_rules_are_independent_per_game_system(tmp_path):
    manager = RulesManager(tmp_path)
    manager.ensure_game_system("Warhammer 40,000")
    manager.app_rule_path("Warhammer 40,000").write_text(json.dumps({
        "game_system": "Warhammer 40,000", "parser": {"type": "gw_app_40k"}}), encoding="utf-8")
    manager.pdf_rule_path("Warhammer 40,000").write_text(json.dumps({
        "game_system": "Warhammer 40,000", "parser": {"type": "warhammer_40k_points_pdf"}, "profiles": []}), encoding="utf-8")
    manager.ensure_game_system("Age of Sigmar")
    manager.app_rule_path("Age of Sigmar").write_text(json.dumps({
        "game_system": "Age of Sigmar", "parser": {"type": "gw_app_aos_inventory"}}), encoding="utf-8")
    manager.pdf_rule_path("Age of Sigmar").write_text(json.dumps({
        "game_system": "Age of Sigmar", "parser": {"type": "aos_battle_profiles_pdf"}, "profiles": []}), encoding="utf-8")

    assert manager.parser_type("Warhammer 40,000", "app_import") == "gw_app_40k"
    assert manager.parser_type("Warhammer 40,000", "pdf_import") == "warhammer_40k_points_pdf"
    assert manager.parser_type("Age of Sigmar", "app_import") == "gw_app_aos_inventory"
    assert manager.parser_type("Age of Sigmar", "pdf_import") == "aos_battle_profiles_pdf"


def test_import_army_reuses_hierarchy_and_adds_models(collection):
    units = (ImportedUnit("Intercessor Squad", 5), ImportedUnit("Captain", 1))
    assert collection.import_army("Warhammer 40,000", "Adeptus Astartes", units) == 6
    assert collection.import_army("warhammer 40,000", "adeptus astartes", (ImportedUnit("Captain", 1),)) == 1
    rows = collection.list_collection()
    assert len(rows) == 7
    assert {row.unit for row in rows} == {"Intercessor Squad", "Captain"}


def test_repeated_imported_unit_names_remain_distinct_units(collection):
    units = (ImportedUnit("Intercessor Squad", 5), ImportedUnit("Intercessor Squad", 5))
    assert collection.import_army("Warhammer 40,000", "Adeptus Astartes", units) == 10
    rows = collection.list_collection()
    unit_ids = {row.unit_id for row in rows}
    assert len(unit_ids) == 2
    assert sorted(sum(row.unit_id == unit_id for row in rows) for unit_id in unit_ids) == [5, 5]


def test_aos_import_uses_rules_unit_size():
    sizes = {"Bull Centaurs": 3, "Hobgrotz Vandalz": 10}
    parsed = parse_gw_army_text("""
Chaos Dwarf Inventory 1000/3000 pts
Helsmiths of Hashut
Bull Centaurs (190)
Hobgrotz Vandalz (70)
""", lambda faction, name: sizes.get(name))
    assert parsed.units == (
        ImportedUnit("Bull Centaurs", 3),
        ImportedUnit("Hobgrotz Vandalz", 10),
    )


def test_app_import_keeps_parsed_counts_when_pdf_profiles_are_missing():
    parsed = parse_gw_army_text("""
Skyfleet Inventory 1000/2000 pts
Kharadron Overlords
3x Grundstok Thunderers (100)
Grimnyr (120)
""", lambda faction, name: None)

    assert parsed.units == (
        ImportedUnit("Grundstok Thunderers", 3),
        ImportedUnit("Grimnyr", 1),
    )
    assert parsed.missing_profiles == ("Grundstok Thunderers", "Grimnyr")


def test_rules_manager_keeps_profiles_in_text_not_database(tmp_path):
    manager = RulesManager(tmp_path)
    manager.rules_path.parent.mkdir(parents=True, exist_ok=True)
    manager.rules_path.write_text(
        '{"profiles":[{"faction":"Helsmiths of Hashut","name":"Bull Centaurs","unit_size":3}]}',
        encoding="utf-8",
    )
    assert manager.unit_size("Helsmiths of Hashut", "Bull Centaurs") == 3


def test_rules_manager_lists_model_profiles_for_only_the_selected_system(tmp_path):
    manager = RulesManager(tmp_path)
    manager.ensure_game_system("Custom System")
    manager.rule_path("Custom System").write_text(
        json.dumps({
            "profiles": [
                {"faction": "Beta", "name": "Second", "unit_size": 1, "points": 20},
                {"faction": "Alpha", "name": "First", "unit_size": 1, "points": 10},
            ]
        }),
        encoding="utf-8",
    )
    manager.ensure_game_system("Other System")
    manager.rule_path("Other System").write_text(
        json.dumps({"profiles": [{"faction": "Other", "name": "Wrong System"}]}),
        encoding="utf-8",
    )
    assert [profile["name"] for profile in manager.list_profiles("Custom System")] == [
        "First", "Second"
    ]
    assert manager.config_path.suffix == ".json"


def test_40k_normal_profile_uses_smallest_unit_bracket_and_keeps_variants_in_json(tmp_path):
    manager = RulesManager(tmp_path)
    path = manager.ensure_game_system("Warhammer 40,000")
    path.write_text(json.dumps({
        "game_system": "Warhammer 40,000",
        "profiles": [
            {"faction": "Adepta Sororitas", "name": "Battle Sisters Squad", "unit_size": 10, "points": 105},
            {"faction": "Adepta Sororitas", "name": "Battle Sisters Squad", "unit_size": 20, "points": 190},
        ],
    }), encoding="utf-8")

    assert manager.unit_size("Adepta Sororitas", "Battle Sisters Squad", "Warhammer 40,000") == 10
    selectable = manager.list_profiles("Warhammer 40,000")
    assert len(selectable) == 1
    assert selectable[0]["unit_size"] == 10
    assert selectable[0]["points"] == 105
    assert len(json.loads(path.read_text(encoding="utf-8"))["profiles"]) == 2


def test_bundled_rules_fix_chaos_dwarf_inventory_model_total(tmp_path):
    manager = RulesManager(tmp_path)
    rows = (
        ["Anointed Sentinels (130)"] * 2
        + ["Ashen Elder (120)"] * 3
        + ["Bull Centaurs (190)"] * 4
        + ["Daemonsmith (80)"] * 3
        + ["Daemonsmith on Infernal Taurus (290)"] * 2
        + ["Deathshrieker Rocket Battery (140)"] * 3
        + ["Dominator Engine with Bane Maces (150)"] * 4
        + ["Dominator Engine with Immolation Cannons (160)"] * 3
        + ["Hobgrotz Vandalz (70)"] * 4
        + ["Infernal Cohort with Hashutite Blades (90)"] * 7
        + ["Infernal Razers with Blunderbusses (110)"] * 6
        + ["Tormentor Bombard (130)", "Urak Taar, the First Daemonsmith (340)"]
        + ["War Despot (80)"] * 4
    )
    source = "Chaos Dwarf Inventory 27680/3000 pts\nHelsmiths of Hashut\n" + "\n".join(rows)
    parsed = parse_gw_army_text(source, manager.unit_size)
    assert len(parsed.units) == 47
    assert sum(unit.model_count for unit in parsed.units) == 182


def test_bulk_update_models_and_assign_locations_in_order(collection):
    _, _, unit_id, first = build_hierarchy(collection)
    second = collection.create_model(unit_id, display_name="Bull Centaur 2")
    third = collection.create_model(unit_id, display_name="Bull Centaur 3")
    count = collection.bulk_update_models(
        [first, second, third], assembly_status="assembled", paint_status="primed",
        is_magnetized=True, location_start=(2, 3, 4),
    )
    assert count == 3
    rows = collection.list_collection()
    assert [row.storage_location for row in rows] == [
        "Cabinet 2 Slot 3", "Cabinet 2 Slot 4", "Cabinet 3 Slot 1"
    ]
    assert all(row.assembly_status == "assembled" for row in rows)
    assert all(row.paint_status == "primed" and row.is_magnetized for row in rows)


def test_bulk_update_models_with_same_cabinet_and_slot(collection):
    _, _, unit_id, first = build_hierarchy(collection)
    second = collection.create_model(unit_id, display_name="Second")
    count = collection.bulk_update_models(
        [first, second], storage_location="Cabinet 7 Slot 12"
    )
    assert count == 2
    assert {row.storage_location for row in collection.list_collection()} == {
        "Cabinet 7 Slot 12"
    }


def test_bulk_update_models_with_same_custom_location(collection):
    _, _, unit_id, first = build_hierarchy(collection)
    second = collection.create_model(unit_id, display_name="Second")
    collection.bulk_update_models([first, second], storage_location="Display case A")
    assert {row.storage_location for row in collection.list_collection()} == {
        "Display case A"
    }


def test_delete_models_deletes_only_selected_rows(collection):
    _, _, unit_id, first = build_hierarchy(collection)
    second = collection.create_model(unit_id, display_name="Second")
    third = collection.create_model(unit_id, display_name="Keep me")
    assert collection.delete_models([first, second]) == 2
    rows = collection.list_collection()
    assert [row.id for row in rows] == [third]


def test_delete_all_models_for_game_system_preserves_hierarchy(collection, database):
    system_id, faction_id, unit_id, first = build_hierarchy(collection)
    collection.create_model(unit_id, display_name="Second")
    other_system = collection.create_game_system("Warhammer 40,000")
    other_faction = collection.create_faction(other_system, "Adeptus Astartes")
    other_unit = collection.create_unit(other_faction, "Intercessors")
    keep = collection.create_model(other_unit, display_name="Keep")

    assert collection.count_models_for_game_system(system_id) == 2
    assert collection.delete_models_for_game_system(system_id) == 2
    assert collection.count_models_for_game_system(system_id) == 0
    assert [model.id for model in collection.list_models()] == [keep]
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM game_system WHERE id = ?", (system_id,)).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM faction WHERE id = ?", (faction_id,)).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM unit WHERE id = ?", (unit_id,)).fetchone()[0] == 1


def test_rules_urls_are_saved_per_game_system(tmp_path):
    from wms.rules import RulesManager

    manager = RulesManager(tmp_path)
    aos_url = "https://example.com/aos.pdf"
    forty_k_url = "https://example.com/40k.pdf"
    manager.set_source_url("Age of Sigmar", aos_url)
    manager.set_source_url("Warhammer 40,000", forty_k_url)

    assert manager.source_url("Age of Sigmar") == aos_url
    assert manager.source_url("Warhammer 40,000") == forty_k_url
    assert manager.source_url("The Old World") == ""


def test_removing_game_system_rules_deletes_file_and_saved_url(tmp_path):
    manager = RulesManager(tmp_path / "rules-root")
    manager.ensure_game_system("AoS")
    manager.set_source_url("AoS", "https://example.com/aos.pdf")

    manager.remove_game_system("AoS")

    assert not manager.rule_path("AoS").exists()
    assert manager.source_url("AoS") == ""


def test_each_game_system_uses_its_own_named_rules_file(tmp_path):
    manager = RulesManager(tmp_path)
    assert manager.app_rule_path("Age of Sigmar").name == "Age of Sigmar.import-rule.json"
    assert manager.pdf_rule_path("Warhammer 40,000").name == "Warhammer 40,000.unit-data.json"
    assert manager.rule_path("Age of Sigmar").parent.name == "Age of Sigmar"
    path = manager.ensure_game_system("Warhammer 40,000")
    assert path.exists()
    assert '"game_system": "Warhammer 40,000"' in path.read_text(encoding="utf-8")


def test_generate_rule_from_sample_text_and_use_for_custom_system(tmp_path):
    manager = RulesManager(tmp_path)
    assert manager.generate_from_text(
        "Horus Heresy", "Legiones Astartes",
        "Tactical Squad (100 Points)\n  • 10x Legionary\nPraetor (120 Points)\n  • 1x Praetor",
    ) == 2
    payload = json.loads(manager.app_rule_path("Horus Heresy").read_text(encoding="utf-8"))
    assert "profiles" not in payload
    assert payload["sample"]["unit_entries"] == 2
    parsed = parse_gw_army_text(
        "Tactical Squad (100 Points)\nPraetor (120 Points)",
        lambda system, faction, unit: manager.unit_size(faction, unit, system),
        "Horus Heresy", "Legiones Astartes",
    )
    assert parsed.units == (ImportedUnit("Tactical Squad", 1), ImportedUnit("Praetor", 1))


def test_generate_rule_from_csv(tmp_path):
    manager = RulesManager(tmp_path)
    source = tmp_path / "rules.csv"
    source.write_text("Faction,Unit,Models,Points\nOrruk Warclans,Ardboyz,10,180\n", encoding="utf-8")
    assert manager.generate_from_csv("Custom System", source) == 1
    assert manager.unit_size("Orruk Warclans", "Ardboyz", "Custom System") == 10


def test_import_rule_file_validates_copies_and_renames_for_selected_system(tmp_path):
    manager = RulesManager(tmp_path / "data")
    source = tmp_path / "downloaded-rule.json"
    source.write_text(json.dumps({
        "format_version": 2,
        "game_system": "Source System",
        "publication": "Community rule",
        "profiles": [{"faction": "Solar Auxilia", "name": "Rifle Section", "unit_size": 20}],
    }), encoding="utf-8")

    target, count = manager.import_rule_file("Horus Heresy", source)

    assert count == 1
    assert target == manager.rules_dir / "Horus Heresy" / "Horus Heresy.unit-data.json"
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["game_system"] == "Horus Heresy"
    assert saved["imported_from"] == "downloaded-rule.json"
    assert manager.unit_size("Solar Auxilia", "Rifle Section", "Horus Heresy") == 20
    assert json.loads(source.read_text(encoding="utf-8"))["game_system"] == "Source System"


def test_import_rule_file_rejects_invalid_profile_without_replacing_target(tmp_path):
    manager = RulesManager(tmp_path / "data")
    target = manager.ensure_game_system("Custom System")
    before = target.read_text(encoding="utf-8")
    source = tmp_path / "invalid.json"
    source.write_text('{"profiles": [{"name": "Broken", "unit_size": 0}]}', encoding="utf-8")

    with pytest.raises(RulesError, match="invalid unit_size"):
        manager.import_rule_file("Custom System", source)

    assert target.read_text(encoding="utf-8") == before


def test_legacy_combined_rule_is_split_without_mixing_sources(tmp_path):
    manager = RulesManager(tmp_path)
    legacy = manager.rules_dir / "Warhammer 40,000.json"
    legacy.write_text(json.dumps({
        "game_system": "Warhammer 40,000",
        "parsers": {
            "app_import": {"type": "gw_app_40k"},
            "pdf_import": {"type": "warhammer_40k_mfm"},
        },
        "app_import_sample": {"unit_entries": 3},
        "profiles": [{"faction": "Necrons", "name": "Warriors", "unit_size": 10, "points": 100}],
    }), encoding="utf-8")

    manager.ensure_game_system("Warhammer 40,000")

    app = json.loads(manager.app_rule_path("Warhammer 40,000").read_text(encoding="utf-8"))
    pdf = json.loads(manager.pdf_rule_path("Warhammer 40,000").read_text(encoding="utf-8"))
    assert app["parser"]["type"] == "gw_app_40k"
    assert app["sample"]["unit_entries"] == 3
    assert "profiles" not in app
    assert pdf["parser"]["type"] == "warhammer_40k_mfm"
    assert pdf["profiles"][0]["name"] == "Warriors"
    assert legacy.with_suffix(".legacy.json").exists()


def test_importing_app_rule_does_not_replace_pdf_profiles(tmp_path):
    manager = RulesManager(tmp_path)
    manager.ensure_game_system("Warhammer 40,000")
    pdf_before = manager.pdf_rule_path("Warhammer 40,000").read_text(encoding="utf-8")
    source = tmp_path / "app.json"
    source.write_text(json.dumps({
        "game_system": "Other", "parser": {"type": "gw_app_40k"},
        "sample": {"unit_entries": 2},
    }), encoding="utf-8")

    target, count = manager.import_rule_file("Warhammer 40,000", source, "app")

    assert target == manager.app_rule_path("Warhammer 40,000")
    assert count == 0
    assert manager.pdf_rule_path("Warhammer 40,000").read_text(encoding="utf-8") == pdf_before


def test_inspecting_unit_json_never_changes_existing_rule(tmp_path):
    manager = RulesManager(tmp_path / "data")
    manager.ensure_game_system("Test System")
    target = manager.pdf_rule_path("Test System")
    before = target.read_bytes()
    source = tmp_path / "units.json"
    source.write_text(json.dumps({
        "document_type": "wms_official_unit_data",
        "profiles": [{"faction": "Test", "name": "Unit A", "unit_size": 3, "points": 100}],
    }), encoding="utf-8")

    payload, count = manager.inspect_rule_file("Test System", source, "pdf")

    assert count == 1
    assert payload["profiles"][0]["name"] == "Unit A"
    assert target.read_bytes() == before


def test_building_parser_extracted_unit_data_is_read_only(tmp_path):
    manager = RulesManager(tmp_path / "data")
    manager.ensure_game_system("Test System")
    target = manager.pdf_rule_path("Test System")
    before = target.read_bytes()
    parsed = ParsedArmy(
        "Test Faction", (ImportedUnit("Unit A", 2, points=90),), "Detected System", ()
    )

    payload, count = manager.build_merged_unit_data("Test System", parsed)

    assert count == 1
    assert payload["profiles"][0]["points"] == 90
    assert target.read_bytes() == before


def test_parser_rule_preview_does_not_save_generated_rule(tmp_path):
    manager = RulesManager(tmp_path / "data")
    manager.ensure_game_system("Warhammer 40,000")
    target = manager.app_rule_path("Warhammer 40,000")
    before = target.read_bytes()
    sample = """Test Inventory (100 points)\nTest Faction\nNo Detachment\nTest Unit (100 points)\n  • 1x Test Model\n    • 1x Test weapon\nExported with App Version: v2.3.1"""

    payload, count = manager.inspect_text_rule("Warhammer 40,000", sample)

    assert count == 1
    assert payload["parser"]["dialect"] == "gw_app_40k"
    assert target.read_bytes() == before


def test_copy_model_uses_new_id_in_same_unit_and_copies_configuration(collection):
    _, _, unit_id, model_id = build_hierarchy(collection)
    collection.update_model(model_id, display_name="Original", assembly_status="assembled",
                            paint_status="painted", is_magnetized=True,
                            storage_location="Case A", notes="Keep")
    collection.create_configuration(
        model_id, "Sword", configuration_type="weapon", loadout_name="Sword",
        notes="Active loadout",
    )
    assert collection.copy_models([model_id, model_id]) == 1
    models = collection.list_models()
    assert len(models) == 2
    copied = next(model for model in models if model.id != model_id)
    assert copied.unit_id == unit_id
    assert copied.display_name == "Original"
    assert copied.paint_status == "painted"
    with collection.database.connect() as connection:
        config = connection.execute(
            "SELECT name, notes FROM configuration WHERE physical_model_id = ?", (copied.id,)
        ).fetchone()
    assert tuple(config) == ("Sword", "Active loadout")


def test_copy_unit_creates_independent_unit_and_models(collection):
    _, _, unit_id, first = build_hierarchy(collection)
    collection.create_model(unit_id, display_name="Second")
    assert collection.copy_units([unit_id, unit_id]) == (1, 2)
    rows = collection.list_collection()
    unit_ids = {row.unit_id for row in rows}
    assert len(unit_ids) == 2
    assert sorted(sum(row.unit_id == value for row in rows) for value in unit_ids) == [2, 2]


def test_copy_unit_preserves_persisted_points(collection):
    _, _, unit_id, _ = build_hierarchy(collection)
    collection.set_unit_points(unit_id, 175, manual=True)
    collection.copy_units([unit_id])
    assert {(row.unit_points, row.unit_points_manual) for row in collection.list_collection()} == {
        (175, True)
    }


def test_copy_unit_preserves_json_model_configuration(collection):
    _, _, unit_id, model_id = build_hierarchy(collection)
    collection.create_configuration(
        model_id, "Current build", configuration_type="model",
        rule_faction="Helsmiths of Hashut", rule_model_name="Bull Centaurs",
        points=140,
    )
    collection.copy_units([unit_id])
    copied_row = next(row for row in collection.list_collection() if row.unit_id != unit_id)
    copied_configuration = collection.list_configurations(copied_row.id)[0]
    assert copied_configuration.rule_model_name == "Bull Centaurs"
    assert copied_configuration.configuration_type == "model"
    assert copied_configuration.is_active is True


def test_delete_units_removes_complete_entries_and_preserves_other_units(collection):
    system, faction, first_unit, first_model = build_hierarchy(collection)
    collection.create_model(first_unit, display_name="Second")
    keep_unit = collection.create_unit(faction, "Keep Unit")
    keep_model = collection.create_model(keep_unit, display_name="Keep Model")

    assert collection.delete_units([first_unit, first_unit]) == (1, 2)
    rows = collection.list_collection()
    assert [(row.unit_id, row.id) for row in rows] == [(keep_unit, keep_model)]
    assert collection.list_named("game_system") == [(system, "Age of Sigmar")]


def test_dashboard_totals_points_once_for_multi_model_unit(tmp_path, collection):
    system, faction, unit, _ = build_hierarchy(collection)
    collection.create_model(unit, display_name="Bull Centaur 2")
    other = collection.create_unit(faction, "Ashen Elder")
    collection.create_model(other)
    manager = RulesManager(tmp_path)
    manager.ensure_game_system("Age of Sigmar")
    manager.pdf_rule_path("Age of Sigmar").write_text(json.dumps({
        "game_system": "Age of Sigmar",
        "profiles": [
            {"faction": "Helsmiths of Hashut", "name": "Bull Centaurs", "unit_size": 3, "points": 180},
            {"faction": "Helsmiths of Hashut", "name": "Ashen Elder", "unit_size": 1, "points": 130},
        ],
    }), encoding="utf-8")
    collection.set_unit_points(unit, 180, manual=False)
    collection.set_unit_points(other, 130, manual=False)

    totals = _dashboard_totals(collection.list_collection(), manager)

    assert len(totals) == 1
    assert (totals[0].unit_instances, totals[0].physical_models, totals[0].points) == (2, 3, 310)


def test_export_faction_database_contains_only_selected_faction(tmp_path, collection):
    system, faction, unit, model = build_hierarchy(collection)
    collection.create_configuration(
        model, "Ashen Elder", configuration_type="model",
        rule_faction="Helsmiths of Hashut", rule_model_name="Ashen Elder", points=130,
    )
    other_faction = collection.create_faction(system, "Orruk Warclans")
    other_unit = collection.create_unit(other_faction, "Ardboyz")
    collection.create_model(other_unit)
    target = tmp_path / "helsmiths.sqlite3"

    assert collection.export_faction_database(
        "Age of Sigmar", "Helsmiths of Hashut", target
    ) == (1, 1)

    with sqlite3.connect(target) as exported:
        assert exported.execute("SELECT name FROM faction").fetchall() == [("Helsmiths of Hashut",)]
        assert exported.execute("SELECT name FROM unit").fetchall() == [("Bull Centaurs",)]
        assert exported.execute("SELECT COUNT(*) FROM physical_model").fetchone()[0] == 1
        assert exported.execute("SELECT COUNT(*) FROM configuration").fetchone()[0] == 1


def test_export_faction_csv_is_filtered_and_excel_friendly(tmp_path, collection):
    system, faction, unit, model = build_hierarchy(collection)
    other_faction = collection.create_faction(system, "Orruk Warclans")
    other_unit = collection.create_unit(other_faction, "Ardboyz")
    collection.create_model(other_unit)
    manager = RulesManager(tmp_path / "rules")
    manager.ensure_game_system("Age of Sigmar")
    manager.pdf_rule_path("Age of Sigmar").write_text(json.dumps({
        "game_system": "Age of Sigmar",
        "profiles": [{
            "faction": "Helsmiths of Hashut", "name": "Bull Centaurs",
            "unit_size": 3, "points": 180,
        }],
    }), encoding="utf-8")
    collection.set_unit_points(unit, 180, manual=False)
    target = tmp_path / "helsmiths.csv"

    assert _export_faction_csv(
        collection, manager, "Age of Sigmar", "Helsmiths of Hashut", target
    ) == (1, 1)

    with target.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["Faction"] == "Helsmiths of Hashut"
    assert rows[0]["Active Model"] == "Bull Centaurs"
    assert rows[0]["Points"] == "180"
    assert rows[0]["Weapon / Loadout"] == "Default"


def test_exported_collection_csv_can_be_imported(tmp_path, collection):
    _, _, _, model = build_hierarchy(collection)
    collection.update_model(
        model, display_name="Bull Centaur A", assembly_status="assembled",
        paint_status="painted", is_magnetized=True, storage_location="Case 1", notes="Champion",
    )
    collection.create_configuration(
        model, "Ashen Elder", configuration_type="model",
        rule_faction="Helsmiths of Hashut", rule_model_name="Ashen Elder",
        points=130, is_active=True, unit_points=130,
    )
    rules = RulesManager(tmp_path / "rules")
    exported = tmp_path / "roundtrip.csv"
    assert _export_faction_csv(
        collection, rules, "Age of Sigmar", "Helsmiths of Hashut", exported
    ) == (1, 1)

    restored_db = Database(tmp_path / "restored.sqlite3")
    restored_db.initialize()
    restored = CollectionService(restored_db)
    restored.create_game_system("Age of Sigmar")
    assert _import_wms_collection_csv(restored, exported) == (1, 1)

    row = restored.list_collection()[0]
    assert row.game_system == "Age of Sigmar"
    assert row.faction == "Helsmiths of Hashut"
    assert row.unit == "Bull Centaurs"
    assert row.display_name == "Bull Centaur A"
    assert row.assembly_status == "assembled"
    assert row.paint_status == "painted"
    assert row.is_magnetized is True
    assert row.storage_location == "Case 1"
    assert row.notes == "Champion"
    assert row.represented_unit == "Ashen Elder"
    assert row.unit_points == 130


def test_manual_rule_json_save_is_validated_and_logged(tmp_path):
    manager = RulesManager(tmp_path)
    manager.ensure_game_system("Age of Sigmar")
    payload = manager.load_rule_json("Age of Sigmar", "pdf")
    payload["profiles"] = [{
        "faction": "Helsmiths of Hashut", "name": "Ashen Elder",
        "unit_size": 1, "points": 130, "source_page": 0, "raw_text": "Manual entry",
    }]

    manager.save_rule_json(
        "Age of Sigmar", "pdf", payload, "Manual edit", "Added Ashen Elder"
    )

    assert manager.load_rule_json("Age of Sigmar", "pdf")["profiles"][0]["name"] == "Ashen Elder"
    assert manager.rule_change_log("Age of Sigmar")[-1]["details"] == "Added Ashen Elder"

    payload["profiles"][0]["points"] = -1
    with pytest.raises(RulesError, match="invalid Model Count or Points"):
        manager.save_rule_json("Age of Sigmar", "pdf", payload, "Manual edit", "Invalid")


def test_game_system_rules_are_bound_one_to_one_by_immutable_id(tmp_path):
    manager = RulesManager(tmp_path)
    first = manager.bind_game_system("id-40k-short", "warhammer 40k")
    second = manager.bind_game_system("id-40k-long", "Warhammer 40,000")

    assert first != second
    assert manager.validate_binding("id-40k-short", "warhammer 40k", "app")["game_system_id"] == "id-40k-short"
    with pytest.raises(RulesError, match="not bound"):
        manager.validate_binding("id-40k-long", "warhammer 40k", "app")


def test_import_json_rejects_wrong_document_type_and_foreign_system_id(tmp_path):
    manager = RulesManager(tmp_path / "data")
    manager.bind_game_system("target-id", "warhammer 40k")
    wrong_type = tmp_path / "unit-data.json"
    wrong_type.write_text(json.dumps({
        "document_type": "wms_official_unit_data", "game_system_id": "target-id",
        "profiles": [],
    }), encoding="utf-8")
    with pytest.raises(RulesError, match="Wrong JSON type"):
        manager.import_rule_file("warhammer 40k", wrong_type, "app")

    foreign = tmp_path / "foreign.json"
    foreign.write_text(json.dumps({
        "document_type": "wms_import_rule", "game_system_id": "another-id",
        "parser": {"type": "declarative", "dialect": "gw_app_40k"},
    }), encoding="utf-8")
    with pytest.raises(RulesError, match="different Game System ID"):
        manager.import_rule_file("warhammer 40k", foreign, "app")


def test_40k_iron_master_models_and_weapons_import_to_selected_system(tmp_path):
    text = """Votan Inventory (7390 points)

Leagues of Votann
No Detachment
Onslaught (3000 points)

Brôkhyr Iron-master (70 points)
  • 1x Brôkhyr Iron-master
    • 1x Graviton hammer
      1x Graviton rifle
  • 1x Ironkin Assistant
    • 1x Close combat weapon
      1x Las-beam cutter
  • 1x E-COG
    • 1x Plasma torch
  • 1x E-COG
    • 1x Manipulator arms
  • 1x E-COG
    • 1x Autoch-pattern bolt pistol
      1x Close combat weapon

Exported with App Version: v2.3.1 (138), Data Version: v913
"""
    parsed = parse_gw_army_text(text, rule_parser_type="gw_app_40k")
    unit = parsed.units[0]
    assert unit.model_count == 5
    assert [model.name for model in unit.physical_models] == [
        "Brôkhyr Iron-master", "Ironkin Assistant", "E-COG", "E-COG", "E-COG",
    ]
    assert unit.physical_models[0].weapons == ("Graviton hammer", "Graviton rifle")

    database = Database(tmp_path / "collection.sqlite3")
    database.initialize()
    service = CollectionService(database)
    system_id = service.create_game_system("warhammer 40k")
    assert service.import_army("warhammer 40k", parsed.faction, parsed.units, system_id) == 5
    assert [row.game_system for row in service.list_collection()] == ["warhammer 40k"] * 5
    rows = service.list_collection()
    assert sorted(row.display_name for row in rows) == sorted([
        "Brôkhyr Iron-master", "Ironkin Assistant", "E-COG 1", "E-COG 2", "E-COG 3",
    ])
    iron_master = next(row for row in rows if row.display_name == "Brôkhyr Iron-master")
    assert iron_master.current_weapon_configuration == "Graviton hammer + Graviton rifle"
    assert all(row.current_points is None for row in rows)
    assert all(row.unit_points == 70 for row in rows)
    assert not any(name == "Warhammer 40,000" for _, name in service.list_named("game_system"))


def test_selected_game_system_id_cannot_be_replaced_by_detected_name(tmp_path):
    database = Database(tmp_path / "collection.sqlite3")
    database.initialize()
    service = CollectionService(database)
    selected_id = service.create_game_system("warhammer 40k")
    parsed = ImportedUnit("Test Unit", 1)
    with pytest.raises(CollectionError, match="does not match"):
        service.import_army("Warhammer 40,000", "Faction", (parsed,), selected_id)


def test_generate_import_rule_creates_replaceable_declarative_json_for_selected_system(tmp_path):
    manager = RulesManager(tmp_path)
    manager.bind_game_system("short-40k-id", "warhammer 40k")
    sample = """Votan Inventory (70 points)

Leagues of Votann
No Detachment
Brôkhyr Iron-master (70 points)
  • 1x Brôkhyr Iron-master
    • 1x Graviton hammer
Exported with App Version: v2.3.1
"""
    assert manager.generate_from_text("warhammer 40k", sample) == 1
    payload = manager.validate_binding("short-40k-id", "warhammer 40k", "app")
    assert payload["document_type"] == "wms_import_rule"
    assert payload["parser"]["type"] == "declarative"
    assert payload["parser"]["dialect"] == "gw_app_40k"
    assert payload["parser"]["logic"]["weapon_assignment"] == "nested_under_model"
    assert manager.app_rule_path("warhammer 40k") == (
        manager.rules_dir / "warhammer 40k" / "warhammer 40k.import-rule.json"
    )


def test_40k_single_model_character_and_vehicle_use_unit_name_not_weapon_name():
    parsed = parse_gw_army_text("""Votan Inventory (320 points)
Leagues of Votann
No Detachment
Arkanyst Evaluator (70 points)
  • 1x Close combat weapon
    1x Transmatter inverter
Hekaton Land Fortress (250 points)
  • 1x Armoured wheels
    1x Cyclic ion cannon
    2x Twin bolt cannon
Exported with App Version: v2.3.1
""", rule_parser_type="gw_app_40k")
    evaluator, vehicle = parsed.units
    assert evaluator.physical_models[0].name == "Arkanyst Evaluator"
    assert evaluator.physical_models[0].weapons == ("Close combat weapon", "Transmatter inverter")
    assert vehicle.physical_models[0].name == "Hekaton Land Fortress"
    assert vehicle.physical_models[0].weapons == (
        "Armoured wheels", "Cyclic ion cannon", "Twin bolt cannon",
    )


def test_40k_app_points_are_saved_even_when_unit_has_no_weapon_rows(tmp_path):
    database = Database(tmp_path / "collection.sqlite3")
    database.initialize()
    service = CollectionService(database)
    system_id = service.create_game_system("40K")
    imported = ImportedUnit("Mystery Unit", 1, points=125)
    assert service.import_army("40K", "Test Faction", (imported,), system_id) == 1
    row = service.list_collection()[0]
    assert row.current_points is None
    assert row.unit_points == 125
    assert row.current_weapon_configuration == "Default"


def test_declarative_parser_json_changes_actual_text_extraction():
    definition = {
        "patterns": {
            "inventory": r"^(.+?) Roster \(\d+ points\)$",
            "unit": r"^UNIT: (.+?) \[(\d+) pts\]$",
            "counted_item": r"^(\s*)(?:[•\-]\s*)?(\d+)x\s+(.+?)\s*$",
        },
        "logic": {
            "faction_detection": "line_after_inventory",
            "model_strategy": "unit_name_single",
            "weapon_assignment": "default",
            "points_detection": "none",
        },
    }
    # Custom Unit regex keeps the engine contract: optional quantity, name, points.
    definition["patterns"]["unit"] = r"^(?:UNIT:)\s*(?:(\d+)x\s+)?(.+?)\s*\[(\d+) pts\]$"
    parsed = parse_gw_army_text(
        "Custom Roster (999 points)\nTest Faction\nUNIT: Test Walker [125 pts]",
        fallback_game_system="Custom", fallback_faction="Test Faction",
        rule_parser_type="gw_app_40k", parser_definition=definition,
    )
    assert parsed.units[0].name == "Test Walker"
    assert parsed.units[0].physical_models[0].name == "Test Walker"
    assert parsed.units[0].points is None


def test_parser_output_is_saved_as_unit_data_not_parser_logic(tmp_path):
    manager = RulesManager(tmp_path)
    manager.bind_game_system("40k-id", "40K")
    parsed = ParsedArmy(
        "Test Faction",
        (ImportedUnit("Test Unit", 2, (
            ImportedPhysicalModel("Leader", 1, ("Sword",)),
            ImportedPhysicalModel("Trooper", 1, ("Rifle",)),
        ), 95),),
        "Warhammer 40,000",
    )
    assert manager.merge_parsed_unit_data("40K", parsed) == 1
    unit_data = manager.load_rule_json("40K", "pdf")
    profile = unit_data["profiles"][0]
    assert profile["name"] == "Test Unit"
    assert profile["points"] == 95
    assert profile["models"][0] == {"name": "Leader", "quantity": 1, "weapons": ["Sword"]}
    parser = manager.load_rule_json("40K", "app")
    assert "profiles" not in parser


@pytest.mark.parametrize("game_system", ["Age of Sigmar", "Warhammer 40K"])
def test_app_missing_points_never_overwrites_unit_data_for_any_system(tmp_path, game_system):
    manager = RulesManager(tmp_path)
    manager.bind_game_system(f"{game_system}-id", game_system)
    unit_data = manager.load_rule_json(game_system, "pdf")
    unit_data["profiles"] = [{
        "faction": "Test Faction", "name": "Test Unit",
        "unit_size": 3, "points": 180,
    }]
    manager.save_rule_json(game_system, "pdf", unit_data, "Test fixture", "Official profile")
    parsed = ParsedArmy(
        "Test Faction", (ImportedUnit("Test Unit", 1, points=None),), game_system,
    )

    payload, _changed = manager.build_merged_unit_data(game_system, parsed)

    profile = next(item for item in payload["profiles"] if item["name"] == "Test Unit")
    assert profile["points"] == 180
    assert manager.points("Test Faction", "Test Unit", game_system) == 180


@pytest.mark.parametrize("game_system", ["Age of Sigmar", "Warhammer 40K"])
def test_unit_data_points_are_bound_to_unit_instance_for_every_system(tmp_path, game_system):
    manager = RulesManager(tmp_path)
    manager.bind_game_system(f"{game_system}-id", game_system)
    payload = manager.load_rule_json(game_system, "pdf")
    payload["profiles"] = [{
        "faction": "Test Faction", "name": "Test Unit",
        "unit_size": 3, "points": 180,
    }]
    manager.save_rule_json(game_system, "pdf", payload, "Fixture", "Official profile")
    parsed = ParsedArmy(
        "Test Faction", (ImportedUnit("Test Unit", 3, points=None),), game_system,
    )

    resolved = _resolve_unit_instance_points(
        parsed, manager, game_system, "Test Faction"
    )
    assert resolved.units[0].points == 180

    database = Database(tmp_path / "collection.sqlite3")
    database.initialize()
    service = CollectionService(database)
    system_id = service.create_game_system(game_system)
    service.import_army(game_system, "Test Faction", resolved.units, system_id)
    rows = service.list_collection()
    assert all(row.unit_points == 180 for row in rows)
    assert all(row.current_points is None for row in rows)
    assert _collection_points(rows[0], manager) == 180


def test_app_explicit_points_still_update_40k_unit_data(tmp_path):
    manager = RulesManager(tmp_path)
    manager.bind_game_system("40k-id", "Warhammer 40K")
    unit_data = manager.load_rule_json("Warhammer 40K", "pdf")
    unit_data["profiles"] = [{
        "faction": "Test Faction", "name": "Test Unit",
        "unit_size": 5, "points": 160,
    }]
    manager.save_rule_json("Warhammer 40K", "pdf", unit_data, "Test fixture", "Official profile")
    parsed = ParsedArmy(
        "Test Faction", (ImportedUnit("Test Unit", 5, points=175),), "Warhammer 40K",
    )

    payload, _changed = manager.build_merged_unit_data("Warhammer 40K", parsed)

    assert payload["profiles"][0]["points"] == 175


def test_collection_binding_uses_unit_data_not_uncommitted_app_points(tmp_path):
    manager = RulesManager(tmp_path)
    manager.bind_game_system("system-id", "Test System")
    payload = manager.load_rule_json("Test System", "pdf")
    payload["profiles"] = [{
        "faction": "Faction", "name": "Unit", "unit_size": 1, "points": 90,
    }]
    manager.save_rule_json("Test System", "pdf", payload, "Fixture", "Confirmed Unit Data")
    parsed = ParsedArmy(
        "Faction", (ImportedUnit("Unit", 1, points=999),), "Test System",
    )

    resolved = _resolve_unit_instance_points(parsed, manager, "Test System", "Faction")

    assert resolved.units[0].points == 90


def test_unit_data_update_flags_database_difference_without_overwriting(tmp_path, collection):
    manager = RulesManager(tmp_path)
    manager.bind_game_system("system-id", "Test System")
    payload = manager.load_rule_json("Test System", "pdf")
    payload["profiles"] = [{
        "faction": "Faction", "name": "Unit", "unit_size": 1, "points": 90,
    }]
    manager.save_rule_json("Test System", "pdf", payload, "Fixture", "Initial Unit Data")
    collection.import_army("Test System", "Faction", (ImportedUnit("Unit", 1, points=90),))

    payload["profiles"][0]["points"] = 110
    manager.save_rule_json("Test System", "pdf", payload, "Fixture", "Updated Unit Data")
    row = collection.list_collection()[0]

    assert row.unit_points == 90  # durable DB cache remains unchanged
    assert _collection_points(row, manager) == 90
    assert _collection_points_mismatch(row, manager) is True
    assert _dashboard_totals([row], manager)[0].points == 90


def test_pdf_merge_updates_matches_adds_new_and_keeps_app_model_details(tmp_path):
    manager = RulesManager(tmp_path)
    manager.bind_game_system("system-id", "Test System")
    current = manager.load_rule_json("Test System", "pdf")
    current["profiles"] = [{
        "faction": "Faction", "name": "Existing", "unit_size": 2, "points": 90,
        "models": [{"name": "Leader", "quantity": 1, "weapons": ["Sword"]}],
        "source_type": "app_text",
    }]
    manager.save_rule_json("Test System", "pdf", current, "Fixture", "App data")
    detected = current | {
        "publication": "August 2026",
        "profiles": [
            {"faction": "Faction", "name": "Existing", "unit_size": 3, "points": 100},
            {"faction": "Faction", "name": "New Unit", "unit_size": 1, "points": 140},
        ],
    }

    merged, added, updated = manager.build_pdf_unit_data_merge("Test System", detected)

    assert (added, updated) == (1, 1)
    existing = next(item for item in merged["profiles"] if item["name"] == "Existing")
    assert (existing["unit_size"], existing["points"]) == (3, 100)
    assert existing["models"][0]["weapons"] == ["Sword"]


def test_both_import_sources_report_new_and_overwritten_units_before_commit(tmp_path):
    manager = RulesManager(tmp_path)
    manager.bind_game_system("system-id", "Test System")
    current = manager.load_rule_json("Test System", "pdf")
    current["profiles"] = [{
        "faction": "Faction", "name": "Existing", "unit_size": 2, "points": 90,
    }]
    manager.save_rule_json("Test System", "pdf", current, "Fixture", "Existing data")

    parsed = ParsedArmy(
        "Faction",
        (ImportedUnit("Existing", 2, points=100), ImportedUnit("App New", 1, points=50)),
        "Test System",
    )
    _app_payload, app_new, app_overwritten = manager.build_merged_unit_data_preview(
        "Test System", parsed
    )
    assert app_new == ["App New"]
    assert app_overwritten == ["Existing"]

    detected = current | {"profiles": [
        {"faction": "Faction", "name": "Existing", "unit_size": 2, "points": 110},
        {"faction": "Faction", "name": "PDF New", "unit_size": 1, "points": 70},
    ]}
    _pdf_payload, pdf_new, pdf_overwritten = manager.build_pdf_unit_data_merge_preview(
        "Test System", detected
    )
    assert pdf_new == ["PDF New"]
    assert pdf_overwritten == ["Existing"]


def test_unit_data_import_history_records_method_and_confirmed_counts(tmp_path):
    manager = RulesManager(tmp_path)
    manager.bind_game_system("system-id", "Test System")
    payload = manager.load_rule_json("Test System", "pdf")
    manager.save_rule_json(
        "Test System", "pdf", payload, "Confirmed import", "Preview confirmed",
        import_method="App Text", new_units=2, overwritten_units=1,
    )

    assert manager.unit_data_import_history("Test System") == [{
        "timestamp": manager.unit_data_import_history("Test System")[0]["timestamp"],
        "rule_source": "pdf_rule.json",
        "action": "Confirmed import",
        "details": "Preview confirmed",
        "import_method": "App Text",
        "new_units": 2,
        "overwritten_units": 1,
    }]


@pytest.mark.parametrize("game_system", ["Age of Sigmar", "Warhammer 40K"])
def test_pdf_first_then_app_adds_actual_details_without_losing_pdf_points(tmp_path, game_system):
    manager = RulesManager(tmp_path)
    manager.bind_game_system(f"{game_system}-id", game_system)
    pdf_data = manager.load_rule_json(game_system, "pdf")
    pdf_data["profiles"] = [{
        "faction": "Faction", "name": "Existing", "unit_size": 3, "points": 180,
        "source_type": "pdf",
    }]
    manager.save_rule_json(game_system, "pdf", pdf_data, "Fixture", "PDF first")
    parsed = ParsedArmy(
        "Faction",
        (ImportedUnit("Existing", 3, (
            ImportedPhysicalModel("Leader", 1, ("Sword",)),
            ImportedPhysicalModel("Trooper", 2, ("Rifle",)),
        ), points=None),),
        game_system,
    )

    merged, changed = manager.build_merged_unit_data(game_system, parsed)

    assert changed == 1
    profile = next(item for item in merged["profiles"] if item["name"] == "Existing")
    assert profile["points"] == 180
    assert profile["models"] == [
        {"name": "Leader", "quantity": 1, "weapons": ["Sword"]},
        {"name": "Trooper", "quantity": 2, "weapons": ["Rifle"]},
    ]


def test_pdf_unit_data_save_never_changes_independent_parser_json(tmp_path):
    manager = RulesManager(tmp_path)
    manager.bind_game_system("system-id", "Test System")
    parser_before = manager.load_rule_json("Test System", "app")
    parser_before["parsers"] = {
        "app_text": {"type": "generic_unit_rows"},
        "pdf": {"type": "existing_pdf_parser"},
    }
    manager.save_rule_json("Test System", "app", parser_before, "Fixture", "Parser")
    parser_bytes = manager.app_rule_path("Test System").read_bytes()

    database = Database(tmp_path / "collection.sqlite3")
    database.initialize()
    service = CollectionService(database)
    system_id = service.create_game_system("Test System")
    faction_id = service.create_faction(system_id, "Faction")
    unit_id = service.create_unit(faction_id, "Owned Unit")
    service.create_model(unit_id, display_name="Owned Model")
    collection_before = service.list_collection()

    unit_data = manager.load_rule_json("Test System", "pdf")
    unit_data["profiles"] = [{
        "faction": "Faction", "name": "PDF Unit", "unit_size": 1, "points": 100,
    }]
    manager.save_rule_json(
        "Test System", "pdf", unit_data, "Confirmed PDF Unit Data update", "One profile",
        import_method="PDF", new_units=1, overwritten_units=0,
    )

    assert manager.app_rule_path("Test System").read_bytes() == parser_bytes
    assert service.list_collection() == collection_before


def test_app_text_unit_data_save_never_changes_parser_or_collection(tmp_path):
    manager = RulesManager(tmp_path)
    manager.bind_game_system("system-id", "Test System")
    parser_before = manager.load_rule_json("Test System", "app")
    parser_before["parsers"] = {
        "app_text": {"type": "generic_unit_rows"},
        "pdf": {"type": "existing_pdf_parser"},
    }
    manager.save_rule_json("Test System", "app", parser_before, "Fixture", "Parser")
    parser_bytes = manager.app_rule_path("Test System").read_bytes()

    database = Database(tmp_path / "collection.sqlite3")
    database.initialize()
    service = CollectionService(database)
    system_id = service.create_game_system("Test System")
    faction_id = service.create_faction(system_id, "Faction")
    unit_id = service.create_unit(faction_id, "Owned Unit")
    service.create_model(unit_id, display_name="Owned Model")
    collection_before = service.list_collection()

    parsed = ParsedArmy(
        "Faction", (ImportedUnit("App Unit", 1, points=125),), "Test System",
    )
    unit_data, added, overwritten = manager.build_merged_unit_data_preview(
        "Test System", parsed
    )
    manager.save_rule_json(
        "Test System", "pdf", unit_data, "Confirmed App Text Unit Data update",
        "One profile", import_method="App Text",
        new_units=len(added), overwritten_units=len(overwritten),
    )

    assert manager.app_rule_path("Test System").read_bytes() == parser_bytes
    assert service.list_collection() == collection_before


def test_duplicate_aos_profiles_resolve_points_for_new_collection_import(tmp_path):
    manager = RulesManager(tmp_path)
    manager.bind_game_system("aos-id", "Age of Sigmar")
    payload = manager.load_rule_json("Age of Sigmar", "pdf")
    payload["profiles"] = [
        {"faction": "Helsmiths Of Hashut", "name": "Daemonsmith", "unit_size": 1, "points": 80},
        {"faction": "Helsmiths Of Hashut", "name": "Daemonsmith", "unit_size": 1, "points": 80},
    ]
    manager.save_rule_json("Age of Sigmar", "pdf", payload, "Fixture", "Duplicate PDF rows")

    parsed = ParsedArmy(
        "Helsmiths of Hashut", (ImportedUnit("Daemonsmith", 1, points=None),),
        "Age of Sigmar",
    )
    resolved = _resolve_unit_instance_points(
        parsed, manager, "Age of Sigmar", "Helsmiths of Hashut"
    )

    assert manager.points("Helsmiths of Hashut", "Daemonsmith", "Age of Sigmar") == 80
    assert resolved.units[0].points == 80


def test_app_profile_without_points_cannot_hide_confirmed_aos_points(tmp_path):
    manager = RulesManager(tmp_path)
    manager.bind_game_system("aos-id", "Age of Sigmar")
    payload = manager.load_rule_json("Age of Sigmar", "pdf")
    payload["profiles"] = [
        {"faction": "Helsmiths of Hashut", "name": "Daemonsmith", "unit_size": 1},
        {"faction": "Helsmiths Of Hashut", "name": "Daemonsmith", "unit_size": 1, "points": 80},
    ]
    manager.save_rule_json(
        "Age of Sigmar", "pdf", payload, "Fixture", "App and official profiles"
    )

    # UI selector deduplication must not become the source for Points lookup.
    assert manager.list_profiles("Age of Sigmar")[0].get("points") in (None, "")
    assert manager.points("Helsmiths of Hashut", "Daemonsmith", "Age of Sigmar") == 80

    parsed = ParsedArmy(
        "Helsmiths of Hashut", (ImportedUnit("Daemonsmith", 1, points=None),),
        "Age of Sigmar",
    )
    assert _resolve_unit_instance_points(
        parsed, manager, "Age of Sigmar", "Helsmiths of Hashut"
    ).units[0].points == 80


def test_multiple_unit_sizes_use_smallest_legal_profile_points(tmp_path):
    manager = RulesManager(tmp_path)
    manager.bind_game_system("system-id", "Test System")
    payload = manager.load_rule_json("Test System", "pdf")
    payload["profiles"] = [
        {"faction": "Faction", "name": "Unit", "unit_size": 10, "points": 110},
        {"faction": "Faction", "name": "Unit", "unit_size": 20, "points": 200},
    ]
    manager.save_rule_json("Test System", "pdf", payload, "Fixture", "Multiple brackets")

    assert manager.points("Faction", "Unit", "Test System") == 110


def test_historical_database_zero_is_preserved_and_flagged(tmp_path, collection):
    manager = RulesManager(tmp_path)
    manager.bind_game_system("aos-id", "Age of Sigmar")
    payload = manager.load_rule_json("Age of Sigmar", "pdf")
    payload["profiles"] = [
        {"faction": "Helsmiths Of Hashut", "name": "Daemonsmith", "unit_size": 1, "points": 80},
        {"faction": "Helsmiths Of Hashut", "name": "Daemonsmith", "unit_size": 1, "points": 80},
    ]
    manager.save_rule_json("Age of Sigmar", "pdf", payload, "Fixture", "Duplicate PDF rows")
    collection.import_army(
        "Age of Sigmar", "Helsmiths of Hashut", (ImportedUnit("Daemonsmith", 1, points=0),)
    )

    row = collection.list_collection()[0]
    assert row.unit_points == 0
    assert _collection_points(row, manager) == 0
    assert _collection_points_mismatch(row, manager) is True


def test_database_zero_is_preserved_when_unit_data_points_are_missing(tmp_path, collection):
    manager = RulesManager(tmp_path)
    manager.bind_game_system("aos-id", "Age of Sigmar")
    payload = manager.load_rule_json("Age of Sigmar", "pdf")
    payload["profiles"] = [{
        "faction": "Helsmiths Of Hashut", "name": "Daemonsmith",
        "unit_size": 1, "points": None,
    }]
    manager.save_rule_json("Age of Sigmar", "pdf", payload, "Fixture", "Missing Points")
    collection.import_army(
        "Age of Sigmar", "Helsmiths of Hashut", (ImportedUnit("Daemonsmith", 1, points=0),)
    )

    row = collection.list_collection()[0]
    assert row.unit_points == 0
    assert _collection_points(row, manager) == 0
    assert _collection_points_mismatch(row, manager) is True
    assert _display_points(_collection_points(row, manager)) == "0"
    assert _display_points(0) == "0"


def test_dashboard_chart_metrics_use_database_collection_rows():
    totals = [
        SimpleNamespace(game_system="AoS", faction="Hashut", unit_instances=2,
                        physical_models=4, points=300),
        SimpleNamespace(game_system="40K", faction="Votann", unit_instances=1,
                        physical_models=2, points=125),
    ]
    rows = [
        SimpleNamespace(game_system="AoS", faction="Hashut", assembly_status="assembled", paint_status="painted"),
        SimpleNamespace(game_system="AoS", faction="Hashut", assembly_status="unassembled", paint_status="primed"),
        SimpleNamespace(game_system="40K", faction="Votann", assembly_status="assembled", paint_status="unpainted"),
    ]

    assert [(item.label, item.value) for item in _dashboard_chart_values(
        rows, totals, "system_points"
    )] == [("AoS", 300), ("40K", 125)]
    assert {(item.label, item.value) for item in _dashboard_chart_values(
        rows, totals, "faction_assembly", "AoS"
    )} == {("Hashut · Assembled", 1), ("Hashut · Not assembled", 1)}
    assert {(item.label, item.value) for item in _dashboard_chart_values(
        rows, totals, "faction_paint", "AoS"
    )} == {("Hashut · Painted", 1), ("Hashut · Not painted", 1)}


def test_points_highlight_filter_selects_yellow_and_non_yellow(tmp_path, collection):
    manager = RulesManager(tmp_path)
    manager.bind_game_system("system-id", "Test System")
    payload = manager.load_rule_json("Test System", "pdf")
    payload["profiles"] = [{
        "faction": "Faction", "name": "Unit", "unit_size": 1, "points": 100,
    }]
    manager.save_rule_json("Test System", "pdf", payload, "Fixture", "Points")
    collection.import_army("Test System", "Faction", (ImportedUnit("Unit", 1, points=90),))
    row = collection.list_collection()[0]

    assert _points_filter_matches(row, manager, "yellow") is True
    assert _points_filter_matches(row, manager, "not_yellow") is False
    collection.set_unit_points(row.unit_id, 100, manual=False)
    row = collection.list_collection()[0]
    assert _points_filter_matches(row, manager, "yellow") is False
    assert _points_filter_matches(row, manager, "not_yellow") is True


def test_import_precheck_uses_selected_game_system_not_detected_label(tmp_path):
    manager = RulesManager(tmp_path)
    manager.bind_game_system("40k-id", "40K")
    payload = manager.load_rule_json("40K", "pdf")
    payload["profiles"] = [{
        "faction": "Leagues of Votann", "name": "Sagitaur", "unit_size": 1, "points": 95,
    }]
    manager.save_rule_json("40K", "pdf", payload, "Fixture", "40K Unit Data")

    assert _target_unit_size(
        manager, "40K", "Leagues of Votann", "Warhammer 40,000",
        "Leagues of Votann", "Sagitaur",
    ) == 1
    parsed = ParsedArmy(
        "Leagues of Votann", (ImportedUnit("Sagitaur", 1, points=None),),
        "Warhammer 40,000",
    )
    assert _missing_unit_points(parsed, manager, "40K", "Leagues of Votann") == ()


def test_local_and_url_pdf_sources_share_the_same_parser(monkeypatch, tmp_path):
    manager = RulesManager(tmp_path)
    manager.bind_game_system("system-id", "Test System")
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"placeholder")
    profile = UnitProfile("Faction", "Unit", 1, 100, 1, "Unit 1 100")
    monkeypatch.setitem(
        rules_module.PDF_PARSERS, "generic_battle_profiles_pdf",
        lambda _path, _definition: ("August 2026", (profile,)),
    )

    local, local_count = manager.inspect_rules_file("Test System", pdf)
    assert local_count == 1
    assert local["source_file"] == "sample.pdf"

    monkeypatch.setattr(
        rules_module.urllib.request, "urlretrieve",
        lambda _url, target: Path(target).write_bytes(b"placeholder"),
    )
    remote, remote_count = manager.inspect_rules_update(
        "Test System", "https://example.com/sample.pdf"
    )
    assert remote_count == 1
    assert remote["source_url"] == "https://example.com/sample.pdf"
    definition, publication, parser_count = manager.inspect_pdf_parser_url(
        "Test System", "https://example.com/parser-sample.pdf"
    )
    assert definition["type"] == "generic_battle_profiles_pdf"
    assert (publication, parser_count) == ("August 2026", 1)
