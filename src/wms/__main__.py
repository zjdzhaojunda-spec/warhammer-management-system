from __future__ import annotations

import sys

from .collection import CollectionService
from .db import Database
from .gui import run_gui
from .paths import database_path, rules_root
from .rules import RulesManager


def main() -> int:
    database = Database(database_path())
    database.initialize()
    service = CollectionService(database)
    rules = RulesManager(rules_root())
    try:
        return run_gui(service, rules)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
