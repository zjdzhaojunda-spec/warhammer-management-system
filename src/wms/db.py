from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE game_system (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL COLLATE NOCASE,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (name)
    );

    CREATE TABLE faction (
        id TEXT PRIMARY KEY,
        game_system_id TEXT NOT NULL REFERENCES game_system(id) ON DELETE CASCADE,
        name TEXT NOT NULL COLLATE NOCASE,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (game_system_id, name)
    );

    CREATE TABLE unit (
        id TEXT PRIMARY KEY,
        faction_id TEXT NOT NULL REFERENCES faction(id) ON DELETE CASCADE,
        name TEXT NOT NULL COLLATE NOCASE,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (faction_id, name)
    );

    CREATE TABLE physical_model (
        id TEXT PRIMARY KEY,
        unit_id TEXT NOT NULL REFERENCES unit(id) ON DELETE CASCADE,
        display_name TEXT,
        assembly_status TEXT NOT NULL DEFAULT 'unassembled'
            CHECK (assembly_status IN ('unassembled', 'partially_assembled', 'assembled')),
        paint_status TEXT NOT NULL DEFAULT 'unpainted'
            CHECK (paint_status IN ('unpainted', 'primed', 'in_progress', 'painted')),
        is_magnetized INTEGER NOT NULL DEFAULT 0 CHECK (is_magnetized IN (0, 1)),
        storage_location TEXT,
        notes TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE configuration (
        id TEXT PRIMARY KEY,
        physical_model_id TEXT NOT NULL REFERENCES physical_model(id) ON DELETE CASCADE,
        name TEXT NOT NULL COLLATE NOCASE,
        is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
        notes TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (physical_model_id, name)
    );

    CREATE INDEX idx_faction_game_system ON faction(game_system_id);
    CREATE INDEX idx_unit_faction ON unit(faction_id);
    CREATE INDEX idx_model_unit ON physical_model(unit_id);
    CREATE INDEX idx_model_status ON physical_model(paint_status, assembly_status);
    CREATE INDEX idx_configuration_model ON configuration(physical_model_id);
    """,
    """
    PRAGMA foreign_keys = OFF;

    CREATE TABLE unit_v2 (
        id TEXT PRIMARY KEY,
        faction_id TEXT NOT NULL REFERENCES faction(id) ON DELETE CASCADE,
        name TEXT NOT NULL COLLATE NOCASE,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE physical_model_v2 (
        id TEXT PRIMARY KEY,
        unit_id TEXT NOT NULL REFERENCES unit_v2(id) ON DELETE CASCADE,
        display_name TEXT,
        assembly_status TEXT NOT NULL DEFAULT 'unassembled'
            CHECK (assembly_status IN ('unassembled', 'partially_assembled', 'assembled')),
        paint_status TEXT NOT NULL DEFAULT 'unpainted'
            CHECK (paint_status IN ('unpainted', 'primed', 'in_progress', 'painted')),
        is_magnetized INTEGER NOT NULL DEFAULT 0 CHECK (is_magnetized IN (0, 1)),
        storage_location TEXT,
        notes TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE configuration_v2 (
        id TEXT PRIMARY KEY,
        physical_model_id TEXT NOT NULL REFERENCES physical_model_v2(id) ON DELETE CASCADE,
        name TEXT NOT NULL COLLATE NOCASE,
        is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
        notes TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (physical_model_id, name)
    );

    INSERT INTO unit_v2 SELECT * FROM unit;
    INSERT INTO physical_model_v2 SELECT * FROM physical_model;
    INSERT INTO configuration_v2 SELECT * FROM configuration;

    DROP TABLE configuration;
    DROP TABLE physical_model;
    DROP TABLE unit;
    ALTER TABLE unit_v2 RENAME TO unit;
    ALTER TABLE physical_model_v2 RENAME TO physical_model;
    ALTER TABLE configuration_v2 RENAME TO configuration;

    CREATE INDEX idx_unit_faction ON unit(faction_id);
    CREATE INDEX idx_model_unit ON physical_model(unit_id);
    CREATE INDEX idx_model_status ON physical_model(paint_status, assembly_status);
    CREATE INDEX idx_configuration_model ON configuration(physical_model_id);
    PRAGMA foreign_keys = ON;
    """,
    """
    ALTER TABLE configuration ADD COLUMN represented_unit_id TEXT
        REFERENCES unit(id) ON DELETE SET NULL;
    ALTER TABLE configuration ADD COLUMN loadout_name TEXT;
    ALTER TABLE configuration ADD COLUMN points INTEGER CHECK (points IS NULL OR points >= 0);

    UPDATE configuration
       SET is_active = 0
     WHERE id NOT IN (
         SELECT MIN(id) FROM configuration GROUP BY physical_model_id
     );

    CREATE UNIQUE INDEX idx_configuration_one_active
        ON configuration(physical_model_id) WHERE is_active = 1;
    CREATE INDEX idx_configuration_represented_unit
        ON configuration(represented_unit_id);
    """,
    """
    ALTER TABLE configuration ADD COLUMN configuration_type TEXT NOT NULL DEFAULT 'weapon'
        CHECK (configuration_type IN ('model', 'weapon'));
    ALTER TABLE configuration ADD COLUMN rule_faction TEXT;
    ALTER TABLE configuration ADD COLUMN rule_model_name TEXT;

    UPDATE configuration
       SET configuration_type = 'model',
           rule_faction = (
               SELECT faction.name
                 FROM unit
                 JOIN faction ON faction.id = unit.faction_id
                WHERE unit.id = configuration.represented_unit_id
           ),
           rule_model_name = (
               SELECT unit.name FROM unit
                WHERE unit.id = configuration.represented_unit_id
           )
     WHERE represented_unit_id IS NOT NULL;
    """,
    """
    DROP INDEX IF EXISTS idx_configuration_one_active;
    CREATE UNIQUE INDEX idx_configuration_one_active_per_type
        ON configuration(physical_model_id, configuration_type) WHERE is_active = 1;
    """,
    """
    ALTER TABLE unit ADD COLUMN reference_code TEXT;
    ALTER TABLE physical_model ADD COLUMN reference_code TEXT;

    UPDATE unit
       SET reference_code = 'U-' || UPPER(SUBSTR(REPLACE(name, ' ', '-'), 1, 28))
                            || '-' || UPPER(SUBSTR(REPLACE(id, '-', ''), 1, 6));
    UPDATE physical_model
       SET reference_code = 'M-' || UPPER(SUBSTR(REPLACE(
               (SELECT name FROM unit WHERE unit.id = physical_model.unit_id), ' ', '-'), 1, 28))
                            || '-' || UPPER(SUBSTR(REPLACE(id, '-', ''), 1, 6));

    CREATE UNIQUE INDEX idx_unit_reference_code ON unit(reference_code);
    CREATE UNIQUE INDEX idx_model_reference_code ON physical_model(reference_code);
    """,
    """
    ALTER TABLE unit ADD COLUMN points INTEGER CHECK (points IS NULL OR points >= 0);
    """,
    """
    ALTER TABLE unit ADD COLUMN points_manual INTEGER NOT NULL DEFAULT 0
        CHECK (points_manual IN (0, 1));
    """,
)


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            current = connection.execute("PRAGMA user_version").fetchone()[0]
            for version, script in enumerate(MIGRATIONS, start=1):
                if version <= current:
                    continue
                connection.executescript(script)
                connection.execute(f"PRAGMA user_version = {version}")

    def schema_version(self) -> int:
        with self.connect() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
