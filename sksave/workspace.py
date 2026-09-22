"""A local copy of an iOS Soul Knight save, plus the catalog it enumerates.

The workspace is deliberately just two things:

    <root>/Documents/...            the documents container
    <root>/prefs.plist              a copy of the PlayerPrefs plist

Everything the editor needs is derived from those files, so the same code works
for any account: the list of heroes, pets, skins, skills, kill effects and the
weapon id space (the save carries the weapon handbook itself) all come out of
the save rather than from a hard-coded table.
"""

from __future__ import annotations

import json
import plistlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import crypto, fields


class SaveError(RuntimeError):
    pass


@dataclass
class Catalog:
    """What this save says exists."""

    uid: str
    heroes: list[str] = field(default_factory=list)
    pets: list[str] = field(default_factory=list)
    skins: dict[str, int] = field(default_factory=dict)          # hero -> highest skin index
    skills: dict[str, int] = field(default_factory=dict)         # hero -> highest skill slot
    weapon_ids: list[str] = field(default_factory=list)          # bare ids, e.g. "006", "shower"
    blueprint_weapons: list[str] = field(default_factory=list)
    kill_effect_ids: list[int] = field(default_factory=list)
    shards: dict[str, Path] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "heroes": len(self.heroes),
            "pets": len(self.pets),
            "heroesWithSkins": len(self.skins),
            "skins": sum(self.skins.values()),
            "skills": len(self.skills),
            "weapons": len(self.weapon_ids),
            "blueprintWeapons": len(self.blueprint_weapons),
            "killEffects": len(self.kill_effect_ids),
            "shards": len(self.shards),
        }


class SaveWorkspace:
    """Read/write access to a local copy of the save."""

    def __init__(self, root: Path, uid: str | None = None) -> None:
        self.root = Path(root)
        self.documents = self.root / fields.DOCUMENTS
        if not self.documents.is_dir():
            raise SaveError(f"{self.documents} is not a Documents directory")
        self.prefs_path = self.root / "prefs.plist"
        self.uid = uid or self._detect_uid()
        if not self.uid:
            raise SaveError("no <type>_<uid>_.data files found - is this a Soul Knight save?")

    # ------------------------------------------------------------------ #
    # discovery
    # ------------------------------------------------------------------ #
    def _detect_uid(self) -> str:
        counts: dict[str, int] = {}
        for path in self.documents.iterdir():
            match = re.fullmatch(r".+_(\d+)_\.data(?:\.new)?", path.name)
            if match:
                counts[match.group(1)] = counts.get(match.group(1), 0) + 1
        if not counts:
            return ""
        return max(counts, key=lambda key: counts[key])

    def shard_path(self, dtype: str, *, must_exist: bool = False) -> Path:
        path = self.documents / fields.UID_SHARD.format(dtype=dtype, uid=self.uid)
        if must_exist and not path.is_file():
            raise SaveError(f"missing shard {path.name}")
        return path

    def all_shards(self) -> dict[str, Path]:
        found: dict[str, Path] = {}
        pattern = re.compile(rf"^(.+?)_{re.escape(self.uid)}_\.data$")
        for path in sorted(self.documents.iterdir()):
            match = pattern.match(path.name)
            if match:
                found[match.group(1)] = path
        return found

    def legacy_files(self) -> list[Path]:
        """Files the game wrote in its unsupported new format."""
        return sorted(
            path
            for path in self.documents.iterdir()
            if path.name.endswith(fields.LEGACY_FORMAT_SUFFIXES)
        )

    # ------------------------------------------------------------------ #
    # reading / writing
    # ------------------------------------------------------------------ #
    def load(self, path: Path) -> Any:
        data, _algorithm = crypto.decrypt_file(Path(path))
        return data

    def dump(self, path: Path, value: Any) -> bytes:
        dtype = crypto.data_type_from_name(Path(path).name)
        blob = crypto.encrypt_shard_data(value, dtype, Path(path))
        if isinstance(blob, str):
            blob = blob.encode("utf-8")
        Path(path).write_bytes(blob)
        return blob

    def load_game(self) -> dict[str, Any]:
        path = self.documents / fields.GAME_DATA
        if not path.is_file():
            raise SaveError("game.data is missing")
        return self.load(path)

    def save_game(self, data: dict[str, Any]) -> bytes:
        return self.dump(self.documents / fields.GAME_DATA, data)

    def load_shard(self, dtype: str) -> Any:
        return self.load(self.shard_path(dtype, must_exist=True))

    def save_shard(self, dtype: str, value: Any) -> bytes:
        return self.dump(self.shard_path(dtype), value)

    def load_prefs(self) -> dict[str, Any]:
        if not self.prefs_path.is_file():
            raise SaveError("prefs.plist is missing")
        return plistlib.loads(self.prefs_path.read_bytes())

    def save_prefs(self, prefs: dict[str, Any]) -> bytes:
        blob = plistlib.dumps(prefs, fmt=plistlib.FMT_BINARY, sort_keys=False)
        self.prefs_path.write_bytes(blob)
        return blob

    # ------------------------------------------------------------------ #
    # catalog
    # ------------------------------------------------------------------ #
    def catalog(self) -> Catalog:
        catalog = Catalog(uid=self.uid, shards=self.all_shards())
        game = self.load_game()

        catalog.heroes = sorted(game.get(fields.GAME_HEROES) or {})
        catalog.pets = sorted(game.get(fields.GAME_PETS) or {})
        for hero, entries in (game.get(fields.GAME_SKINS) or {}).items():
            catalog.skins[hero] = len(entries or [])
        for hero, entries in (game.get(fields.GAME_SKILLS) or {}).items():
            catalog.skills[hero] = len(entries or [])

        if self.prefs_path.is_file():
            prefs = self.load_prefs()
            for key in prefs:
                match = fields.HERO_SKIN.match(key)
                if match:
                    hero, skin = int(match.group("hero")), int(match.group("skin"))
                    catalog.skins[f"c{hero}"] = max(catalog.skins.get(f"c{hero}", 0), skin + 1)

        catalog.kill_effect_ids = self._kill_effect_ids()

        weapons: set[str] = set()
        statistic = catalog.shards.get("statistic")
        if statistic:
            try:
                data = self.load(statistic)
                handbook = data.get("handbookWeaponCollectionTrackedLevels") or {}
                weapons |= {
                    key.split("weapon_", 1)[1]
                    for key in fields.weapon_ids_from_handbook(handbook)
                }
            except (SaveError, ValueError):
                pass
        item_path = catalog.shards.get("item_data")
        if item_path:
            try:
                blueprints = (self.load(item_path) or {}).get(fields.ITEM_BLUEPRINTS) or {}
                catalog.blueprint_weapons = sorted(
                    key[len(fields.BLUEPRINT_WEAPON):]
                    for key in blueprints
                    if key.startswith(fields.BLUEPRINT_WEAPON)
                )
                weapons |= set(catalog.blueprint_weapons)
            except (SaveError, ValueError):
                pass
        weapons |= {f"{index:03d}" for index in fields.NUMERIC_WEAPON_RANGE}
        catalog.weapon_ids = sorted(weapons)
        return catalog

    def _kill_effect_ids(self) -> list[int]:
        path = self.all_shards().get("misc_data")
        if not path:
            return []
        try:
            data = self.load(path) or {}
        except (SaveError, ValueError):
            return []
        return sorted(int(value) for value in (data.get(fields.MISC_KILL_EFFECTS) or []))

    # ------------------------------------------------------------------ #
    def describe(self) -> dict[str, Any]:
        game = self.load_game()
        prefs: dict[str, Any] = self.load_prefs() if self.prefs_path.is_file() else {}
        catalog = self.catalog()
        return {
            "uid": self.uid,
            "heroes": catalog.summary()["heroes"],
            "pets": catalog.summary()["pets"],
            "skins": catalog.summary()["skins"],
            "skills": catalog.summary()["skills"],
            "weapons": catalog.summary()["weapons"],
            "killEffects": catalog.summary()["killEffects"],
            "gems": game.get("gems"),
            "fishChip": game.get(fields.GAME_FISH_CHIP),
            "legacyFlags": {
                key.format(uid=self.uid): prefs.get(key.format(uid=self.uid))
                for key in fields.LEGACY_FLAG_KEYS
            },
            "legacyFiles": [path.name for path in self.legacy_files()],
            "shards": sorted(catalog.shards),
        }

    @staticmethod
    def dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)
