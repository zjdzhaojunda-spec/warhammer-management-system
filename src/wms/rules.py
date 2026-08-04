from __future__ import annotations

import json
import difflib
import re
import tempfile
import urllib.request
import csv
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


DEFAULT_AOS_BATTLE_PROFILES_URL = (
    "https://assets.warhammer-community.com/"
    "eng_29-07_warhammer_age_of_sigmar_core_rules_battle_profiles-"
    "mhh9urxjwe-tagofadxuo.pdf"
)


class RulesError(ValueError):
    """Raised when a rules source cannot be downloaded or understood safely."""


@dataclass(frozen=True)
class UnitProfile:
    faction: str
    name: str
    unit_size: int
    points: int
    source_page: int
    raw_text: str


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def parse_battle_profiles_pdf(pdf_path: Path, definition=None) -> tuple[str, tuple[UnitProfile, ...]]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RulesError('pypdf is required. Run: python -m pip install -e ".[gui]"') from exc

    reader = PdfReader(str(pdf_path))
    profiles: list[UnitProfile] = []
    publication = "Unknown"
    current_faction = ""
    patterns = (definition or {}).get("patterns", {})
    logic = (definition or {}).get("logic", {})
    faction_pattern = re.compile(patterns.get("faction", r"^[A-Z][A-Z &'\-–]+$"))
    row_pattern = re.compile(patterns.get("profile_row", r"^(.+?)\s+(\d+)\s+(\d+)(?:\s+|$)"))
    table_start = tuple(logic.get("table_start_contains", ["UNIT SIZE", "POINTS"]))
    table_stop = tuple(logic.get("table_stop_contains", ["TYPE", "POINTS"]))

    for page_number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        lines = text.splitlines()
        if page_number == 1:
            match = re.search(r"\b(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+20\d{2}\b", text)
            if match:
                publication = match.group(0).title()

        for line in lines:
            stripped = line.strip()
            if faction_pattern.fullmatch(stripped) and stripped not in {
                "BATTLE PROFILES", "ORDER", "CHAOS", "DEATH", "DESTRUCTION",
                "WARHAMMER LEGENDS", "REGIMENTS OF RENOWN",
            } and "LEGENDS" not in stripped and not any(
                marker in stripped for marker in ("UNIT SIZE", "POINTS", "BASE SIZE", "HEROES", "UNITS", "TYPE", "NOTES")
            ):
                current_faction = stripped.title()

        if not current_faction:
            continue
        in_profile_table = False
        previous = ""
        for line in lines:
            stripped = line.strip(" \t\x07")
            if all(marker in stripped for marker in table_start) and not stripped.startswith("TYPE"):
                in_profile_table = True
                previous = ""
                continue
            if in_profile_table and stripped.startswith(table_stop[0]) and all(
                marker in stripped for marker in table_stop[1:]
            ):
                in_profile_table = False
            if not in_profile_table:
                previous = stripped
                continue
            match = row_pattern.match(stripped)
            if match:
                name, size, points = match.groups()
                prefix = previous
                if (
                    prefix
                    and not re.search(r"\d|mm|POINTS|UNIT SIZE|BASE SIZE", prefix, re.IGNORECASE)
                    and not prefix.startswith(("Any ", "This ", "You ", "General", "regiment", "battlepack"))
                    and len(prefix) < 70
                ):
                    name = f"{prefix} {name}"
                profiles.append(UnitProfile(
                    faction=current_faction,
                    name=re.sub(r"\s+", " ", name.replace("\xa0", " ")).strip(),
                    unit_size=int(size),
                    points=int(points),
                    source_page=page_number,
                    raw_text=(f"{prefix} | " if name.startswith(prefix) and prefix else "") + stripped,
                ))
            previous = stripped

    deduplicated: dict[tuple[str, str], UnitProfile] = {}
    for profile in profiles:
        deduplicated.setdefault((_key(profile.faction), _key(profile.name)), profile)
    if not deduplicated:
        raise RulesError("No unit profiles were found in the selected PDF.")
    return publication, tuple(deduplicated.values())


PDF_PARSERS = {
    # Parser names live in each Game System JSON. Separate implementations can be
    # added without changing App-import dispatch or another system's rule file.
    "aos_battle_profiles_pdf": parse_battle_profiles_pdf,
    "generic_battle_profiles_pdf": parse_battle_profiles_pdf,
}


class RulesManager:
    def __init__(self, root: Path):
        self.root = root
        self.config_path = root / "config" / "urls.json"
        self.rules_dir = root / "import_rules"
        self.bindings_path = self.rules_dir / "bindings.json"
        self.rules_path = self.pdf_rule_path("Age of Sigmar")
        self._ensure_files()

    def _ensure_files(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.rules_dir.mkdir(parents=True, exist_ok=True)
        legacy_rules_dir = self.root / "rules"
        if legacy_rules_dir.exists():
            for legacy_path in legacy_rules_dir.glob("*.json"):
                target = self.rules_dir / legacy_path.name
                if not target.exists():
                    legacy_path.replace(target)
        if not self.config_path.exists():
            self.set_aos_url(DEFAULT_AOS_BATTLE_PROFILES_URL)

    def system_rules_dir(self, game_system: str) -> Path:
        clean = game_system.strip()
        if not clean:
            raise RulesError("Game system must not be empty")
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", clean).rstrip(". ")
        return self.rules_dir / safe

    @staticmethod
    def _safe_system_name(game_system: str) -> str:
        return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", game_system.strip()).rstrip(". ")

    def _bindings(self) -> dict[str, str]:
        try:
            payload = json.loads(self.bindings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def bind_game_system(self, game_system_id: str, game_system: str) -> Path:
        """Bind a rules directory to one immutable database Game System ID."""
        system_id, system = game_system_id.strip(), game_system.strip()
        if not system_id or not system:
            raise RulesError("Game System ID and name are required")
        bindings = self._bindings()
        owner = next((name for name, value in bindings.items()
                      if value == system_id and name != system.casefold()), None)
        if owner:
            del bindings[owner]
        previous = self.system_rules_dir(system)
        bindings[system.casefold()] = system_id
        target = self.rules_dir / self._safe_system_name(system)
        if previous != target and previous.exists() and not target.exists():
            previous.rename(target)
        target.mkdir(parents=True, exist_ok=True)
        self.bindings_path.write_text(json.dumps(bindings, indent=2) + "\n", encoding="utf-8")
        self.ensure_game_system(system)
        for source in ("app", "pdf"):
            path = self.rule_path(system, source)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["game_system_id"] = system_id
            payload["game_system_name"] = system
            payload["game_system"] = system  # compatibility with 0.99 files
            payload["document_type"] = ("wms_import_rule" if source == "app"
                                        else "wms_official_unit_data")
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        app_path, unit_path = self.app_rule_path(system), self.pdf_rule_path(system)
        app_payload = json.loads(app_path.read_text(encoding="utf-8"))
        unit_payload = json.loads(unit_path.read_text(encoding="utf-8"))
        sources = app_payload.get("parsers", {}) if isinstance(app_payload.get("parsers"), dict) else {}
        sources.setdefault("app_text", app_payload.get("parser", {"type": ""}))
        sources.setdefault("pdf", unit_payload.get("parser", {"type": ""}))
        app_payload["parsers"] = sources
        app_path.write_text(json.dumps(app_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return target

    def validate_binding(self, game_system_id: str, game_system: str, rule_source: str) -> dict:
        payload = self.load_rule_json(game_system, rule_source)
        if payload.get("game_system_id") != game_system_id:
            raise RulesError("This JSON is not bound to the selected Game System ID.")
        expected = "wms_import_rule" if rule_source.casefold().startswith("app") else "wms_official_unit_data"
        if payload.get("document_type") != expected:
            raise RulesError(f"Wrong JSON type. Expected {expected}.")
        return payload

    def app_rule_path(self, game_system: str) -> Path:
        directory = self.system_rules_dir(game_system)
        return directory / f"{self._safe_system_name(game_system)}.import-rule.json"

    def pdf_rule_path(self, game_system: str) -> Path:
        directory = self.system_rules_dir(game_system)
        return directory / f"{self._safe_system_name(game_system)}.unit-data.json"

    def rule_path(self, game_system: str, rule_source: str = "pdf") -> Path:
        """Return one source-specific rule path; default remains PDF for old callers."""
        if rule_source.casefold() in {"app", "app_text", "app_import"}:
            return self.app_rule_path(game_system)
        if rule_source.casefold() in {"pdf", "official", "pdf_import"}:
            return self.pdf_rule_path(game_system)
        raise RulesError("Rule source must be App Text or PDF / Official Data.")

    def load_rule_json(self, game_system: str, rule_source: str) -> dict:
        self.ensure_game_system(game_system)
        path = self.rule_path(game_system, rule_source)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RulesError(f"Could not read {path.name}: {exc}") from exc
        if not isinstance(payload, dict):
            raise RulesError("A rule JSON file must contain one JSON object")
        return payload

    def save_rule_json(self, game_system: str, rule_source: str, payload: dict,
                       action: str, details: str, *, import_method: str = "",
                       new_units: int = 0, overwritten_units: int = 0) -> Path:
        """Validate, atomically save, and audit a manual rule-file change."""
        if not isinstance(payload, dict):
            raise RulesError("A rule JSON file must contain one JSON object")
        system = game_system.strip()
        if str(payload.get("game_system_name", payload.get("game_system", system))).strip().casefold() != system.casefold():
            raise RulesError("The JSON game_system must match the selected Game System")
        if rule_source.casefold() in {"pdf", "official", "pdf_import"}:
            profiles = payload.get("profiles")
            if not isinstance(profiles, list):
                raise RulesError("PDF / Official Data JSON must contain a profiles list")
            for number, profile in enumerate(profiles, start=1):
                if not isinstance(profile, dict):
                    raise RulesError(f"Profile {number} must be a JSON object")
                if not str(profile.get("faction", "")).strip() or not str(profile.get("name", "")).strip():
                    raise RulesError(f"Profile {number} requires Faction and Unit Name")
                try:
                    if int(profile.get("unit_size", 0)) < 1:
                        raise ValueError
                    points = profile.get("points")
                    if points not in (None, "") and int(points) < 0:
                        raise ValueError
                except (TypeError, ValueError) as exc:
                    raise RulesError(f"Profile {number} has invalid Model Count or Points") from exc
        path = self.rule_path(system, rule_source)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".json.tmp"
        ) as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.replace(path)
        log_entry = {
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            "rule_source": "app_rule.json" if rule_source.casefold().startswith("app") else "pdf_rule.json",
            "action": action.strip() or "Manual save",
            "details": details.strip(),
        }
        if import_method:
            log_entry.update({
                "import_method": import_method.strip(),
                "new_units": int(new_units),
                "overwritten_units": int(overwritten_units),
            })
        with (path.parent / "change_log.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        return path

    def rule_change_log(self, game_system: str) -> list[dict]:
        path = self.system_rules_dir(game_system) / "change_log.jsonl"
        if not path.exists():
            return []
        entries = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
        return entries

    def unit_data_import_history(self, game_system: str) -> list[dict]:
        """Return successful Unit Data imports, oldest first."""
        return [
            entry for entry in self.rule_change_log(game_system)
            if str(entry.get("import_method", "")).strip()
        ]

    def ensure_game_system(self, game_system: str) -> Path:
        """Create a named, editable rules text file for a game system."""
        system = game_system.strip()
        directory = self.system_rules_dir(system)
        directory.mkdir(parents=True, exist_ok=True)
        app_path = self.app_rule_path(system)
        pdf_path = self.pdf_rule_path(system)
        old_app = directory / "app_rule.json"
        old_pdf = directory / "pdf_rule.json"
        old_modern_app = directory / "import_rule.json"
        old_modern_pdf = directory / "official_unit_data.json"
        for old, new in ((old_app, app_path), (old_pdf, pdf_path),
                         (old_modern_app, app_path), (old_modern_pdf, pdf_path)):
            if old.exists() and not new.exists():
                old.rename(new)
        legacy_path = self.rules_dir / f"{directory.name}.json"
        if legacy_path.exists() and not (app_path.exists() or pdf_path.exists()):
            try:
                legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                legacy = {}
            parsers = legacy.get("parsers", {}) if isinstance(legacy.get("parsers"), dict) else {}
            legacy_parser = legacy.get("parser", {}) if isinstance(legacy.get("parser"), dict) else {}
            app_parser = parsers.get("app_import", legacy_parser)
            pdf_parser = parsers.get("pdf_import", {})
            app_payload = {
                "format_version": 4, "game_system": system, "rule_source": "app_text",
                "parser": app_parser if isinstance(app_parser, dict) else {"type": ""},
                "sample": legacy.get("app_import_sample", {}),
                "updated_at": legacy.get("updated_at", ""),
            }
            pdf_payload = {
                "format_version": 4, "game_system": system, "rule_source": "pdf_official_data",
                "parser": pdf_parser if isinstance(pdf_parser, dict) else {"type": ""},
                "source_url": legacy.get("source_url", ""),
                "publication": legacy.get("publication", "Not scanned"),
                "updated_at": legacy.get("updated_at", ""),
                "profiles": legacy.get("profiles", []),
            }
            app_path.write_text(json.dumps(app_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            pdf_path.write_text(json.dumps(pdf_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            legacy_path.rename(legacy_path.with_suffix(".legacy.json"))
        if not app_path.exists():
            app_type = "gw_app_aos_inventory" if system.casefold() == "age of sigmar" else (
                "gw_app_40k" if system.casefold() == "warhammer 40,000" else ""
            )
            app_path.write_text(json.dumps({
                "format_version": 4, "game_system": system, "rule_source": "app_text",
                "parser": {"type": app_type}, "sample": {}, "updated_at": "",
            }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if not pdf_path.exists():
            bundled = Path(__file__).with_name("default_rules") / "Age of Sigmar.json"
            if system.casefold() == "age of sigmar" and bundled.exists():
                payload = json.loads(bundled.read_text(encoding="utf-8"))
                parser = payload.get("parsers", {}).get("pdf_import", {"type": "aos_battle_profiles_pdf"})
                payload.update({"format_version": 4, "game_system": system,
                                "rule_source": "pdf_official_data", "parser": parser})
                payload.pop("parsers", None)
                pdf_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                return pdf_path
            payload = {
                "format_version": 4, "game_system": system,
                "rule_source": "pdf_official_data", "parser": {"type": ""},
                "source_url": "",
                "publication": "Not scanned",
                "updated_at": "",
                "profiles": [],
            }
            pdf_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return pdf_path

    def remove_game_system(self, game_system: str, game_system_id: str = "") -> None:
        """Remove an obsolete system's rules file and saved source URL."""
        system = game_system.strip()
        directory = self.system_rules_dir(system)
        if directory.exists():
            for path in directory.iterdir():
                if path.is_file():
                    path.unlink()
            directory.rmdir()
        bindings = self._bindings()
        bindings.pop(system.casefold(), None)
        if game_system_id:
            bindings = {name: value for name, value in bindings.items() if value != game_system_id}
        self.bindings_path.write_text(json.dumps(bindings, indent=2) + "\n", encoding="utf-8")
        legacy = self.rules_dir / f"{directory.name}.json"
        if legacy.exists():
            legacy.unlink()
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        urls = data.get("game_system_urls")
        changed = False
        if isinstance(urls, dict):
            for key in list(urls):
                if key.casefold() == system.casefold():
                    del urls[key]
                    changed = True
        if changed:
            self.config_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )

    def source_url(self, game_system: str) -> str:
        system = game_system.strip()
        if not system:
            raise RulesError("Game system must not be empty")
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            urls = data.get("game_system_urls", {})
            if isinstance(urls, dict) and system in urls:
                return str(urls[system])
            if system.casefold() == "age of sigmar":
                return str(data.get("aos_battle_profiles_pdf", ""))
            return ""
        except (OSError, json.JSONDecodeError):
            return ""

    def aos_url(self) -> str:
        return self.source_url("Age of Sigmar")

    def set_source_url(self, game_system: str, url: str) -> None:
        system = game_system.strip()
        if not system:
            raise RulesError("Game system must not be empty")
        clean = url.strip()
        if clean and not clean.startswith(("https://", "http://")):
            raise RulesError("Battle Profiles URL must begin with http:// or https://")
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        urls = data.get("game_system_urls")
        if not isinstance(urls, dict):
            urls = {}
        if clean:
            urls[system] = clean
        else:
            for key in list(urls):
                if key.casefold() == system.casefold():
                    del urls[key]
        data["game_system_urls"] = urls
        self.config_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def set_aos_url(self, url: str) -> None:
        self.set_source_url("Age of Sigmar", url)

    def update_rules(self, game_system: str) -> int:
        payload, count = self.inspect_rules_update(game_system)
        self.save_rule_json(
            game_system, "pdf", payload, "Confirmed PDF scan",
            f"Replaced Official Unit Data with {count} validated profiles",
        )
        return count

    def _inspect_pdf_unit_data(self, game_system: str, pdf_path: Path,
                               *, source_url: str = "", source_file: str = "") -> tuple[dict, int]:
        """Parse one local PDF into a Unit Data preview without writing files."""
        system = game_system.strip()
        parser_type = self.parser_type(system, "pdf_import") or (
            "aos_battle_profiles_pdf" if system.casefold() == "age of sigmar"
            else "generic_battle_profiles_pdf"
        )
        parser = PDF_PARSERS.get(parser_type)
        if parser is None:
            raise RulesError(f"The PDF parser '{parser_type}' is not installed for {system}.")
        pdf_definition = self.parser_definition(system, "pdf")
        publication, profiles = parser(Path(pdf_path), pdf_definition)
        payload = {
            "format_version": 4,
            "game_system": system,
            "game_system_name": system,
            "game_system_id": self._bindings().get(system.casefold(), ""),
            "document_type": "wms_official_unit_data",
            "rule_source": "pdf_official_data",
            "parser": {"type": parser_type},
            "source_url": source_url,
            "publication": publication,
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "profiles": [asdict(profile) for profile in profiles],
        }
        if source_file:
            payload["source_file"] = source_file
        return payload, len(profiles)

    def inspect_rules_file(self, game_system: str, pdf_path: Path) -> tuple[dict, int]:
        """Parse a user-selected local PDF into a Unit Data preview."""
        path = Path(pdf_path)
        if not path.is_file():
            raise RulesError("The selected local PDF does not exist.")
        return self._inspect_pdf_unit_data(game_system, path, source_file=path.name)

    def inspect_rules_update(self, game_system: str, source_url: str | None = None) -> tuple[dict, int]:
        """Download and parse official data into memory without updating stored rules."""
        system = game_system.strip()
        url = (source_url if source_url is not None else self.source_url(system)).strip()
        if not url:
            raise RulesError(f"No Battle Profiles URL is saved for {system}.")
        with tempfile.TemporaryDirectory(prefix="wms_rules_") as directory:
            target = Path(directory) / "battle_profiles.pdf"
            try:
                urllib.request.urlretrieve(url, target)
            except Exception as exc:
                raise RulesError(f"Could not download Battle Profiles PDF: {exc}") from exc
            return self._inspect_pdf_unit_data(system, target, source_url=url)

    def inspect_pdf_parser_url(self, game_system: str, source_url: str) -> tuple[dict, str, int]:
        """Download a representative PDF URL and preview its Parser definition."""
        url = source_url.strip()
        if not url.startswith(("https://", "http://")):
            raise RulesError("PDF URL must begin with http:// or https://")
        with tempfile.TemporaryDirectory(prefix="wms_parser_") as directory:
            target = Path(directory) / "parser_sample.pdf"
            try:
                urllib.request.urlretrieve(url, target)
            except Exception as exc:
                raise RulesError(f"Could not download Parser PDF: {exc}") from exc
            return self.inspect_pdf_parser(game_system, target)

    def build_pdf_unit_data_merge_preview(
        self, game_system: str, detected: dict
    ) -> tuple[dict, list[str], list[str]]:
        """Merge detected PDF facts in memory, retaining App-only model/loadout details."""
        current = self.load_rule_json(game_system, "pdf")
        existing = [dict(item) for item in current.get("profiles", []) if isinstance(item, dict)]
        positions = {
            (_key(str(item.get("faction", ""))), _key(str(item.get("name", "")))): index
            for index, item in enumerate(existing)
        }
        added: list[str] = []
        updated: list[str] = []
        for incoming in detected.get("profiles", []):
            if not isinstance(incoming, dict):
                continue
            key = (_key(str(incoming.get("faction", ""))), _key(str(incoming.get("name", ""))))
            if key in positions:
                index = positions[key]
                # PDF replaces shared official fields while keys only known to
                # App Text (models/weapons) remain attached to the Unit Profile.
                existing[index] = existing[index] | dict(incoming)
                updated.append(str(incoming.get("name", "")).strip())
            else:
                positions[key] = len(existing)
                existing.append(dict(incoming))
                added.append(str(incoming.get("name", "")).strip())
        merged = dict(current)
        for key in ("publication", "source_url", "updated_at", "parser"):
            if key in detected:
                merged[key] = detected[key]
        merged["profiles"] = existing
        return merged, added, updated

    def build_pdf_unit_data_merge(self, game_system: str, detected: dict) -> tuple[dict, int, int]:
        merged, added, updated = self.build_pdf_unit_data_merge_preview(game_system, detected)
        return merged, len(added), len(updated)

    def update_aos_rules(self) -> int:
        return self.update_rules("Age of Sigmar")

    def generate_from_text(self, game_system: str, faction_or_text: str, text: str | None = None) -> int:
        """Generate a per-system rule file from a representative army-list sample."""
        from .gw_import import parse_gw_army_text
        from .gw_import.common import normalized_lines, parse_unit_rows

        # Keep the old three-argument API compatible while making Faction optional.
        faction = faction_or_text.strip() if text is not None else ""
        sample = text if text is not None else faction_or_text
        system = game_system.strip()
        parser_type = "generic_unit_rows"
        if system.casefold() == "warhammer 40,000":
            parsed = parse_gw_army_text(sample)
            units = parsed.units
            faction = parsed.faction
            parser_type = "gw_app_40k"
        else:
            try:
                parsed = parse_gw_army_text(sample)
            except Exception:
                units = parse_unit_rows(normalized_lines(sample))
            else:
                units = parsed.units
                faction = parsed.faction or faction
                parser_type = (
                    "gw_app_aos_inventory" if parsed.game_system.casefold() == "age of sigmar"
                    else "gw_app_40k" if parsed.game_system.casefold() == "warhammer 40,000"
                    else "generic_unit_rows"
                )
        return self._write_app_generated(game_system, "Sample text", units, faction, parser_type)

    def inspect_text_rule(self, game_system: str, sample: str) -> tuple[dict, int]:
        """Build a generated App Parser Rule in memory for Preview/confirmation."""
        from .gw_import import parse_gw_army_text
        from .gw_import.common import normalized_lines, parse_unit_rows

        system = game_system.strip()
        parser_type = "generic_unit_rows"
        faction = ""
        if system.casefold() == "warhammer 40,000":
            parsed = parse_gw_army_text(sample)
            units, faction, parser_type = parsed.units, parsed.faction, "gw_app_40k"
        else:
            try:
                parsed = parse_gw_army_text(sample)
            except Exception:
                units = parse_unit_rows(normalized_lines(sample))
            else:
                units, faction = parsed.units, parsed.faction
                parser_type = (
                    "gw_app_aos_inventory" if parsed.game_system.casefold() == "age of sigmar"
                    else "gw_app_40k" if parsed.game_system.casefold() == "warhammer 40,000"
                    else "generic_unit_rows"
                )
        if not units:
            raise RulesError("The sample did not contain any valid Unit entries.")
        return self._write_app_generated(
            system, "Sample text", units, faction, parser_type, dry_run=True
        )

    def inspect_pdf_parser(self, game_system: str, pdf_path: Path) -> tuple[dict, str, int]:
        """Detect a usable PDF parser in memory without updating Parser or Unit Data."""
        system = game_system.strip()
        parser_type = (
            "aos_battle_profiles_pdf" if system.casefold() == "age of sigmar"
            else "generic_battle_profiles_pdf"
        )
        definition = {"type": parser_type}
        publication, profiles = PDF_PARSERS[parser_type](pdf_path, definition)
        return definition, publication, len(profiles)

    def generate_from_csv(self, game_system: str, csv_path: Path) -> int:
        """Generate rules from CSV columns named faction, unit/name, models/unit_size, points."""
        profiles: list[UnitProfile] = []
        try:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                for row_number, row in enumerate(reader, start=2):
                    normalized = {_key(str(key)): value for key, value in row.items() if key}
                    name = normalized.get("unit") or normalized.get("unit name") or normalized.get("name")
                    size = normalized.get("models") or normalized.get("model count") or normalized.get("unit size")
                    if not name or not size:
                        continue
                    profiles.append(UnitProfile(
                        str(normalized.get("faction", "")).strip(), str(name).strip(), int(size),
                        int(normalized.get("points") or 0), 0, f"CSV row {row_number}",
                    ))
        except (OSError, ValueError, csv.Error) as exc:
            raise RulesError(f"Could not read the CSV rule source: {exc}") from exc
        if not profiles:
            raise RulesError("No CSV rows with Unit/Name and Models/Unit Size columns were found.")
        return self._write_generated(game_system, csv_path.name, profiles, "profile_source", "csv_profiles")

    def generate_from_pdf(self, game_system: str, pdf_path: Path) -> int:
        pdf_type = self.parser_type(game_system, "pdf_import")
        if not pdf_type:
            pdf_type = "aos_battle_profiles_pdf" if game_system.strip().casefold() == "age of sigmar" else "generic_battle_profiles_pdf"
        parser = PDF_PARSERS.get(pdf_type)
        if parser is None:
            raise RulesError(f"The PDF parser '{pdf_type}' is not installed for {game_system}.")
        publication, profiles = parser(pdf_path, self.parser_definition(game_system, "pdf"))
        return self._write_generated(
            game_system, f"{pdf_path.name} · {publication}", profiles, "pdf_import", pdf_type
        )

    def import_rule_file(self, game_system: str, source_path: Path,
                         rule_source: str = "pdf") -> tuple[Path, int]:
        """Validate and copy an existing JSON rule into the selected system slot."""
        payload, count = self.inspect_rule_file(game_system, source_path, rule_source)
        target = self.commit_inspected_rule(game_system, payload, rule_source, source_path.name)
        return target, count

    def inspect_rule_file(self, game_system: str, source_path: Path,
                          rule_source: str = "pdf") -> tuple[dict, int]:
        """Validate and normalize an imported JSON entirely in memory."""
        system = game_system.strip()
        if not system:
            raise RulesError("Choose a Game System first.")
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8-sig"))
        except OSError as exc:
            raise RulesError(f"Could not read the selected rule file: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RulesError(
                f"The selected file is not valid JSON (line {exc.lineno}, column {exc.colno})."
            ) from exc
        if not isinstance(payload, dict):
            raise RulesError("The import rule must be a JSON object.")
        is_app = rule_source.casefold() in {"app", "app_text", "app_import"}
        profiles = payload.get("profiles", [])
        if not is_app:
            if not isinstance(profiles, list):
                raise RulesError("The PDF / Official Data rule must contain a profiles list.")
            for index, profile in enumerate(profiles, start=1):
                if not isinstance(profile, dict):
                    raise RulesError(f"Profile {index} must be a JSON object.")
                if not str(profile.get("name", "")).strip():
                    raise RulesError(f"Profile {index} is missing a unit name.")
                try:
                    if int(profile.get("unit_size", 0)) < 1:
                        raise ValueError
                except (TypeError, ValueError):
                    raise RulesError(f"Profile {index} has an invalid unit_size.") from None
                profile.setdefault("points", 0)
        binding = self._bindings().get(system.casefold())
        expected_type = "wms_import_rule" if is_app else "wms_official_unit_data"
        document_type = payload.get("document_type")
        if document_type and document_type != expected_type:
            raise RulesError(f"Wrong JSON type. Expected {expected_type}.")
        source_id = str(payload.get("game_system_id", "")).strip()
        if source_id and binding and source_id != binding:
            raise RulesError("This JSON belongs to a different Game System ID.")
        payload["game_system"] = system
        payload["game_system_name"] = system
        payload["game_system_id"] = binding or source_id
        payload["document_type"] = expected_type
        parsers = payload.pop("parsers", {})
        parser = payload.get("parser")
        if not isinstance(parser, dict):
            slot = "app_import" if is_app else "pdf_import"
            parser = parsers.get(slot, {}) if isinstance(parsers, dict) else {}
        payload["parser"] = parser if isinstance(parser, dict) else {"type": ""}
        payload["rule_source"] = "app_text" if is_app else "pdf_official_data"
        payload["format_version"] = 4
        if is_app:
            payload.pop("profiles", None)
        payload["imported_from"] = source_path.name
        payload["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        return payload, 0 if is_app else len(profiles)

    def commit_inspected_rule(self, game_system: str, payload: dict,
                              rule_source: str, source_name: str = "") -> Path:
        """Commit a previously inspected rule after the UI's explicit confirmation."""
        system = game_system.strip()
        is_app = rule_source.casefold() in {"app", "app_text", "app_import"}
        expected_type = "wms_import_rule" if is_app else "wms_official_unit_data"
        if payload.get("document_type") != expected_type:
            raise RulesError(f"Wrong JSON type. Expected {expected_type}.")
        binding = self._bindings().get(system.casefold())
        if binding and payload.get("game_system_id") != binding:
            raise RulesError("The inspected JSON no longer matches the selected Game System ID.")
        self.ensure_game_system(system)
        return self.save_rule_json(
            system, "app" if is_app else "pdf", payload, "Imported JSON",
            source_name or str(payload.get("imported_from", "Imported file")),
        )

    def _write_generated(self, game_system: str, source: str, profiles, parser_slot: str,
                         parser_type: str = "profile_lookup") -> int:
        system = game_system.strip()
        if not system:
            raise RulesError("Choose a Game System first.")
        deduplicated = {
            (_key(profile.faction), _key(profile.name), profile.unit_size, profile.points): profile
            for profile in profiles
        }
        if not deduplicated:
            raise RulesError("No unit rules were found.")
        self.ensure_game_system(system)
        path = self.pdf_rule_path(system)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        payload.update({
            "format_version": 4,
            "game_system": system,
            "rule_source": "pdf_official_data",
            "source_type": source,
            "parser": {"type": parser_type},
            "publication": "Generated import rule",
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "profiles": [asdict(profile) for profile in deduplicated.values()],
        })
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return len(deduplicated)

    def _write_app_generated(self, game_system: str, source: str, units, faction: str,
                             parser_type: str, dry_run: bool = False):
        """Save an App parser sample without changing PDF-derived profile data."""
        system = game_system.strip()
        if not system:
            raise RulesError("Choose a Game System first.")
        if not dry_run:
            self.ensure_game_system(system)
        path = self.app_rule_path(system)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        payload.update({
            "format_version": 4,
            "game_system": system,
            "rule_source": "app_text",
            "parser": {
                "type": "declarative", "dialect": parser_type,
                "patterns": ({
                    "inventory": r"^\s*(.+?)\s+Inventory\s*\(\s*\d+\s+points?\s*\)\s*$",
                    "faction": r"^\s*\+?\s*FACTION(?:\s+KEYWORD)?\s*:\s*(.+?)\s*$",
                    "unit": r"^\s*(?:(\d+)x\s+)?(.+?)\s*\((\d+)\s*(?:points?|pts?)?\)\s*$",
                    "counted_item": r"^(\s*)(?:[•\-]\s*)?(\d+)x\s+(.+?)\s*$",
                } if parser_type == "gw_app_40k" else {
                    "inventory": r"^\s*.+?\s+Inventory\s+\d+\s*/\s*\d+\s*(?:points?|pts?)\s*$",
                    "unit": r"^\s*(?:(\d+)x\s+)?(.+?)\s*\((\d+)\s*(?:points?|pts?)?\)\s*$",
                    "counted_item": r"^(\s*)[•\-]\s*(\d+)x\s+(.+?)\s*$",
                }),
                "logic": ({
                    "unit_detection": "points_heading",
                    "model_strategy": "individual_model_blocks",
                    "quantity_detection": "count_prefix",
                    "weapon_assignment": "nested_under_model",
                    "points_detection": "unit_heading",
                    "faction_detection": "label_or_line_after_inventory",
                    "unknown_line_behavior": "ignore_with_preview_context",
                } if parser_type == "gw_app_40k" else {
                    "unit_detection": "points_heading",
                    "model_strategy": "official_unit_size_default_models",
                    "quantity_detection": "official_data_then_count_prefix",
                    "weapon_assignment": "default",
                    "points_detection": "official_data",
                    "faction_detection": "line_after_inventory",
                    "unknown_line_behavior": "ignore_with_preview_context",
                }),
            },
            "sample": {
                "source_type": source,
                "detected_faction": faction,
                "unit_entries": len(units),
                "unit_names": sorted({unit.name for unit in units}, key=str.casefold),
            },
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        })
        payload.pop("profiles", None)
        existing_sources = payload.get("parsers", {}) if isinstance(payload.get("parsers"), dict) else {}
        existing_sources["app_text"] = payload["parser"]
        existing_sources.setdefault("pdf", {
            "type": "declarative", "dialect": "aos_battle_profiles_pdf",
            "patterns": {
                "faction": r"^[A-Z][A-Z &'\-–]+$",
                "profile_row": r"^(.+?)\s+(\d+)\s+(\d+)(?:\s+|$)",
            },
            "logic": {
                "table_start_contains": ["UNIT SIZE", "POINTS"],
                "table_stop_contains": ["TYPE", "POINTS"],
                "columns": {"unit": 1, "unit_size": 2, "points": 3},
                "wrapped_unit_names": "merge_previous_text_line",
                "invalid_rows": "skip",
            },
        })
        payload["parsers"] = existing_sources
        if dry_run:
            return payload, len(units)
        self.save_rule_json(
            system, "app", payload, "Created Parser Rule",
            f"Generated {parser_type} from {len(units)} detected sample Unit entries",
        )
        return len(units)

    def parser_type(self, game_system: str, parser_slot: str = "app_import") -> str:
        """Read one App/PDF parser declaration from only the selected Game System."""
        path = self.app_rule_path(game_system)
        if not path.exists():
            return ""
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        if str(payload.get("game_system", game_system)).strip().casefold() != game_system.strip().casefold():
            return ""
        sources = payload.get("parsers", {})
        source = "app_text" if parser_slot == "app_import" else "pdf"
        parser = sources.get(source, {}) if isinstance(sources, dict) else {}
        if not parser and parser_slot == "app_import":
            parser = payload.get("parser", {})
        if not parser and parser_slot != "app_import":
            try:
                legacy = json.loads(self.pdf_rule_path(game_system).read_text(encoding="utf-8"))
                parser = legacy.get("parser", {})
            except (OSError, json.JSONDecodeError):
                parser = {}
        if not isinstance(parser, dict):
            return ""
        return (str(parser.get("dialect", "")) if parser.get("type") == "declarative"
                else str(parser.get("type", "")))

    def parser_definition(self, game_system: str, source: str = "app_text") -> dict:
        """Return the selected system's complete executable App parser definition."""
        payload = self.load_rule_json(game_system, "app")
        sources = payload.get("parsers", {})
        parser = sources.get(source, {}) if isinstance(sources, dict) else {}
        if not parser and source == "app_text":
            parser = payload.get("parser", {})
        return dict(parser) if isinstance(parser, dict) else {}

    def merge_parsed_unit_data(self, game_system: str, parsed) -> int:
        """Persist actual unit facts extracted by a parser, never parser logic."""
        payload, changed = self.build_merged_unit_data(game_system, parsed)
        self.save_rule_json(
            game_system, "pdf", payload, "Parser extraction",
            f"Merged {changed} Unit profiles extracted from App Text",
        )
        return changed

    def build_merged_unit_data_preview(
        self, game_system: str, parsed
    ) -> tuple[dict, list[str], list[str]]:
        """Build a Unit Data update in memory without changing files or the database."""
        payload = self.load_rule_json(game_system, "pdf")
        profiles = [dict(item) for item in payload.get("profiles", []) if isinstance(item, dict)]
        by_key = {
            (_key(str(item.get("faction", ""))), _key(str(item.get("name", "")))): index
            for index, item in enumerate(profiles)
        }
        added: list[str] = []
        updated: list[str] = []
        for unit in parsed.units:
            profile = {
                "faction": parsed.faction,
                "name": unit.name,
                "unit_size": unit.model_count,
                "models": [
                    {"name": model.name, "quantity": model.quantity, "weapons": list(model.weapons)}
                    for model in unit.physical_models
                ],
                "source_type": "app_text",
                "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            # Missing App fields must never erase official PDF facts. In
            # particular, AoS App exports commonly identify a Unit without
            # carrying its official points or warscroll model count.
            if unit.points is not None:
                profile["points"] = unit.points
            key = (_key(parsed.faction), _key(unit.name))
            if key in by_key:
                existing = profiles[by_key[key]]
                if game_system.strip().casefold() == "age of sigmar":
                    profile.pop("unit_size", None)
                profiles[by_key[key]] = existing | profile
                updated.append(unit.name)
            else:
                profile.setdefault("points", None)
                by_key[key] = len(profiles)
                profiles.append(profile)
                added.append(unit.name)
        payload["profiles"] = profiles
        payload["publication"] = payload.get("publication") or "Parser-extracted Unit Data"
        return payload, added, updated

    def build_merged_unit_data(self, game_system: str, parsed) -> tuple[dict, int]:
        payload, added, updated = self.build_merged_unit_data_preview(game_system, parsed)
        return payload, len(added) + len(updated)

    def status(self, game_system: str = "Age of Sigmar") -> str:
        rules_path = self.pdf_rule_path(game_system)
        if not rules_path.exists():
            return "No Battle Profiles rules have been scanned yet."
        try:
            data = json.loads(rules_path.read_text(encoding="utf-8"))
            return f"{data.get('publication', 'Unknown')} · {len(data.get('profiles', []))} profiles · updated {data.get('updated_at', 'unknown')}"
        except (OSError, json.JSONDecodeError):
            return "The Battle Profiles rules text is unreadable; update it again."

    def unit_size(self, faction: str, unit_name: str, game_system: str = "Age of Sigmar") -> int | None:
        rules_path = self.pdf_rule_path(game_system)
        if not rules_path.exists():
            bundled = Path(__file__).with_name("default_rules") / "Age of Sigmar.json"
            if game_system.strip().casefold() == "age of sigmar" and bundled.exists():
                rules_path = bundled
            else:
                return None
        try:
            profiles = json.loads(rules_path.read_text(encoding="utf-8")).get("profiles", [])
        except (OSError, json.JSONDecodeError):
            return None
        faction_key, unit_key = _key(faction), _key(unit_name)
        exact = [p for p in profiles if _key(str(p.get("name", ""))) == unit_key]
        faction_matches = [p for p in exact if _key(str(p.get("faction", ""))) == faction_key]
        match = (faction_matches or exact)
        if len(match) == 1:
            return int(match[0]["unit_size"])
        sizes = [int(profile["unit_size"]) for profile in match if profile.get("unit_size")]
        if sizes:
            # The normal profile is the smallest legal unit bracket. Larger MFM
            # brackets remain in JSON but never silently replace this default.
            return min(sizes)
        same_faction = [p for p in profiles if _key(str(p.get("faction", ""))) == faction_key]
        close = [
            p for p in same_faction
            if difflib.SequenceMatcher(None, unit_key, _key(str(p.get("name", "")))).ratio() >= 0.94
        ]
        return int(close[0]["unit_size"]) if len(close) == 1 else None

    def points(self, faction: str, unit_name: str, game_system: str = "Age of Sigmar") -> int | None:
        """Return normal/base Points from confirmed Unit Data.

        Do not use ``list_profiles()`` here.  That method intentionally reduces
        duplicate names for UI selectors and can retain an App-derived profile
        with no Points ahead of the official profile.  Points resolution must
        inspect every confirmed Unit Data row before choosing a legal bracket.
        """
        try:
            profiles = self.list_unit_data_profiles(game_system)
        except RulesError:
            return None
        faction_key, unit_key = _key(faction), _key(unit_name)
        exact = [
            profile for profile in profiles
            if _key(str(profile.get("name", ""))) == unit_key
        ]
        faction_matches = [
            profile for profile in exact
            if _key(str(profile.get("faction", ""))) == faction_key
        ]
        def normal_points(matches: list[dict[str, object]]) -> int | None:
            """Resolve duplicate/profile-bracket rows without inventing a value.

            Battle Profiles PDFs can yield the same row more than once, and some
            systems publish several legal sizes for one Unit.  The normal Unit
            profile is the smallest legal size, matching ``unit_size()``.  Exact
            duplicates are harmless; conflicting Points for that same smallest
            size remain unresolved so the caller can warn instead of guessing.
            """
            candidates: list[tuple[int, int]] = []
            for profile in matches:
                value = profile.get("points")
                if value in (None, ""):
                    continue
                try:
                    candidates.append((int(profile.get("unit_size") or 0), int(value)))
                except (TypeError, ValueError):
                    continue
            if not candidates:
                return None
            positive_sizes = [size for size, _points in candidates if size > 0]
            normal_size = min(positive_sizes) if positive_sizes else 0
            values = {points for size, points in candidates if size == normal_size}
            return next(iter(values)) if len(values) == 1 else None

        if faction_matches:
            return normal_points(faction_matches)
        same_faction = [
            profile for profile in profiles
            if _key(str(profile.get("faction", ""))) == faction_key
        ]
        close = [
            profile for profile in same_faction
            if difflib.SequenceMatcher(
                None, unit_key, _key(str(profile.get("name", "")))
            ).ratio() >= 0.94
        ]
        # Fuzzy matching may also encounter duplicate PDF rows.  Only accept a
        # single normalized Unit name, then apply the same bracket resolution.
        close_names = {_key(str(profile.get("name", ""))) for profile in close}
        return normal_points(close) if len(close_names) == 1 else None

    def list_unit_data_profiles(self, game_system: str) -> list[dict[str, object]]:
        """Return every valid profile stored in confirmed Unit Data, without deduping."""
        payload = self.load_rule_json(game_system, "pdf")
        profiles = payload.get("profiles", [])
        if not isinstance(profiles, list):
            raise RulesError("The selected Game System Unit Data has no valid profiles list.")
        return [
            profile for profile in profiles
            if isinstance(profile, dict) and str(profile.get("name", "")).strip()
        ]

    def list_profiles(self, game_system: str) -> list[dict[str, object]]:
        """Return selectable model profiles from one Game System's JSON rule file."""
        path = self.rule_path(game_system)
        if not path.exists():
            path = self.ensure_game_system(game_system)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RulesError("The selected Game System rule file is unreadable.") from exc
        profiles = payload.get("profiles", [])
        if not isinstance(profiles, list):
            raise RulesError("The selected Game System rule file has no valid profiles list.")
        valid = [
            profile for profile in profiles
            if isinstance(profile, dict) and str(profile.get("name", "")).strip()
        ]
        normal: dict[tuple[str, str], dict[str, object]] = {}
        for profile in valid:
            key = (_key(str(profile.get("faction", ""))), _key(str(profile.get("name", ""))))
            current = normal.get(key)
            def bracket(item):
                try:
                    return (int(item.get("unit_size", 0) or 0), int(item.get("points", 0) or 0))
                except (TypeError, ValueError):
                    return (999999, 999999)
            if current is None or bracket(profile) < bracket(current):
                normal[key] = profile
        return sorted(
            normal.values(),
            key=lambda profile: (
                str(profile.get("faction", "")).casefold(),
                str(profile.get("name", "")).casefold(),
            ),
        )
