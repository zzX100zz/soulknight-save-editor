"""Apply the unlock edits to a local copy of the save.

The rules here are the part that was verified in-game on version 8.5.1:

* ``game.data`` (XOR): skins, hero/skill unlocks, hero levels, currencies.
* ``item_data`` (DES-iambo): weapon blueprints in the "Researched" state,
  evolution blueprints, mythic levels, material/seed/ticket quantities.
* ``weapon_evolution_data`` (DES-iambo): ``Level: 1`` per weapon plus its skins.
* ``misc_data``: kill-effect ids, ``season_data``: season coin.
* PlayerPrefs: the same unlocks as typed plist values, the two format switches
  set back to 0, and the ``first_active`` mirror.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from . import fields
from .workspace import SaveWorkspace

Log = Callable[[str], None]


@dataclass
class UnlockOptions:
    """What to unlock.  Everything is on by default; caps can be lowered."""

    heroes: bool = True
    hero_levels: bool = True
    skins: bool = True
    pets: bool = True
    skills: bool = True
    weapons: bool = True
    weapon_skins: bool = True
    evolution: bool = True
    kill_effects: bool = True
    mythic: bool = True
    materials: bool = True
    season_coin: bool = True
    gems: bool = True
    repair_format: bool = True

    hero_level: int = fields.HERO_LEVEL_MAX
    quantity: int = fields.MAX_QUANTITY
    gems_value: int = fields.GEM_MAX
    season_coin_value: int = fields.SEASON_COIN_MAX


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _weapon_skin_indices(documents: Path) -> dict[str, list[int]]:
    """``weapon_<id>`` -> skin indices, read from the game's downloaded assets."""
    found: dict[str, list[int]] = {}
    assets = documents / "hot_update" / "data" / "skin" / "weapon"
    if not assets.is_dir():
        return found
    for directory in assets.glob("weapon_*"):
        indices = {
            int(match.group(1))
            for path in directory.glob("skin_*")
            if (match := re.match(r"skin_(\d+)", path.name))
        }
        if indices:
            found[directory.name] = sorted(indices)
    return found


def _weapon_key_forms(tail: str) -> list[str]:
    return fields.weapon_key_forms(tail)


# --------------------------------------------------------------------------- #
# per-file edits
# --------------------------------------------------------------------------- #
def patch_game(workspace: SaveWorkspace, catalog, options: UnlockOptions, report: dict) -> None:
    game = workspace.load_game()
    stats: dict[str, Any] = {}

    if options.skins:
        changed = 0
        for entries in (game.get(fields.GAME_SKINS) or {}).values():
            for entry in entries or []:
                if entry.get("Value") != fields.OWNED_NUMBER:
                    entry["Value"] = fields.OWNED_NUMBER
                    changed += 1
        stats["skinsUnlocked"] = changed

    if options.skills:
        changed = 0
        for entries in (game.get(fields.GAME_SKILLS) or {}).values():
            for entry in entries or []:
                if entry.get("Value") is not True:
                    entry["Value"] = True
                    changed += 1
        stats["skillsUnlocked"] = changed

    if options.heroes:
        changed = 0
        for hero, value in list((game.get(fields.GAME_HEROES) or {}).items()):
            if value is not True:
                game[fields.GAME_HEROES][hero] = True
                changed += 1
        stats["heroesUnlocked"] = changed

    if options.pets:
        changed = 0
        for pet, value in list((game.get(fields.GAME_PETS) or {}).items()):
            if value is not True:
                game[fields.GAME_PETS][pet] = True
                changed += 1
        stats["petsUnlocked"] = changed

    if options.hero_levels:
        changed = 0
        for hero, level in list((game.get(fields.GAME_HERO_LEVELS) or {}).items()):
            if int(level or 0) < options.hero_level:
                game[fields.GAME_HERO_LEVELS][hero] = options.hero_level
                changed += 1
        stats["heroLevelsRaised"] = changed

    if options.gems:
        before = {key: game.get(key) for key in ("gems", "lastGems")}
        gems = max(int(game.get("gems") or 0), options.gems_value)
        game["gems"] = gems
        game["lastGems"] = gems
        stats["gems"] = {"before": before, "after": gems}

    if options.materials:
        before = int(game.get(fields.GAME_FISH_CHIP) or 0)
        game[fields.GAME_FISH_CHIP] = max(before, options.quantity)
        stats["fishChip"] = {"before": before, "after": game[fields.GAME_FISH_CHIP]}

    workspace.save_game(game)
    report["game"] = stats


def patch_item(workspace: SaveWorkspace, catalog, options: UnlockOptions, report: dict) -> None:
    path = workspace.shard_path("item_data", must_exist=True)
    item = workspace.load(path)
    stats: dict[str, Any] = {}
    blueprints: dict[str, Any] = item.setdefault(fields.ITEM_BLUEPRINTS, {})

    if options.materials:
        for name in fields.ITEM_QUANTITY_FIELDS:
            table = item.get(name)
            if not isinstance(table, dict):
                continue
            raised = 0
            for key, value in list(table.items()):
                if not isinstance(value, (int, float)) or int(value) < options.quantity:
                    table[key] = options.quantity
                    raised += 1
            stats[f"{name}Raised"] = raised

    if options.mythic:
        mythic = item.setdefault(fields.ITEM_MYTHIC, [])
        known = {int(entry.get("id")): entry for entry in mythic if isinstance(entry, dict)}
        for weapon_id in fields.MYTHIC_ID_RANGE:
            entry = known.get(weapon_id)
            if entry is None:
                mythic.append({"id": weapon_id, "level": fields.MYTHIC_LEVEL_MAX})
            else:
                entry["level"] = fields.MYTHIC_LEVEL_MAX
        stats["mythicWeapons"] = len(mythic)

    if options.weapons:
        added = promoted = 0
        for tail in catalog.weapon_ids:
            for form in _weapon_key_forms(tail):
                key = fields.BLUEPRINT_WEAPON + form
                if key not in blueprints:
                    blueprints[key] = fields.BLUEPRINT_UNLOCKED
                    added += 1
                elif blueprints[key] != fields.BLUEPRINT_UNLOCKED:
                    blueprints[key] = fields.BLUEPRINT_UNLOCKED
                    promoted += 1
        for key in list(blueprints):
            if key.startswith(fields.BLUEPRINT_WEAPON) and blueprints[key] != fields.BLUEPRINT_UNLOCKED:
                blueprints[key] = fields.BLUEPRINT_UNLOCKED
                promoted += 1
        stats["weaponBlueprints"] = {
            "added": added,
            "promotedToResearched": promoted,
            "unlockedTotal": sum(
                1 for key, value in blueprints.items()
                if key.startswith(fields.BLUEPRINT_WEAPON) and value == fields.BLUEPRINT_UNLOCKED
            ),
        }

    if options.evolution:
        added = 0
        for tail in catalog.weapon_ids:
            for form in _weapon_key_forms(tail):
                key = fields.BLUEPRINT_EVOLUTION + form
                if key not in blueprints:
                    blueprints[key] = fields.BLUEPRINT_DROPPED
                    added += 1
        stats["evolutionBlueprintsAdded"] = added

    if options.weapon_skins:
        if item.get(fields.ITEM_HAND_STYLES) != fields.HAND_STYLES:
            item[fields.ITEM_HAND_STYLES] = list(fields.HAND_STYLES)
        stats["handStyles"] = fields.HAND_STYLES

    workspace.dump(path, item)
    report["item"] = stats


def patch_evolution(workspace: SaveWorkspace, catalog, options: UnlockOptions, report: dict) -> None:
    path = workspace.shard_path("weapon_evolution_data", must_exist=True)
    data = workspace.load(path)
    weapons: dict[str, Any] = data.setdefault(fields.EVOLUTION_WEAPONS, {})
    harvested = _weapon_skin_indices(workspace.documents) if options.weapon_skins else {}

    added = levelled = skins_added = 0
    for tail in catalog.weapon_ids:
        for form in _weapon_key_forms(tail):
            wid = f"weapon_{form}"
            entry = weapons.get(wid)
            if entry is None:
                entry = {"Name": wid, "Level": 1, "CurrentSkinIndex": 0, "UnlockedSkins": []}
                weapons[wid] = entry
                added += 1
            if int(entry.get("Level") or 0) != 1:
                entry["Level"] = 1
                levelled += 1
            if options.weapon_skins:
                wanted = {f"{wid}_s_{index}" for index in harvested.get(wid, [1])}
                wanted.add(f"{wid}_s_1")
                current = list(entry.get("UnlockedSkins") or [])
                merged = current + sorted(skin for skin in wanted if skin not in current)
                if len(merged) != len(current):
                    skins_added += len(merged) - len(current)
                    entry["UnlockedSkins"] = merged

    for entry in weapons.values():          # weapons the save knows but the catalog missed
        if int(entry.get("Level") or 0) != 1:
            entry["Level"] = 1
            levelled += 1

    workspace.dump(path, data)
    report["evolution"] = {
        "weaponsAdded": added,
        "levelsForced": levelled,
        "weaponSkinsAdded": skins_added,
        "weaponsTotal": len(weapons),
        "weaponSkinsTotal": sum(len(entry.get("UnlockedSkins") or []) for entry in weapons.values()),
    }


def patch_misc(workspace: SaveWorkspace, catalog, options: UnlockOptions, report: dict) -> None:
    if not options.kill_effects:
        return
    path = workspace.shard_path("misc_data", must_exist=True)
    data = workspace.load(path)
    before = list(data.get(fields.MISC_KILL_EFFECTS) or [])
    merged = sorted({int(value) for value in before} | set(fields.KILL_EFFECT_ID_RANGE))
    data[fields.MISC_KILL_EFFECTS] = merged
    workspace.dump(path, data)
    report["misc"] = {"killEffectsBefore": len(before), "killEffectsAfter": len(merged)}


def patch_season(workspace: SaveWorkspace, catalog, options: UnlockOptions, report: dict) -> None:
    if not options.season_coin:
        return
    path = workspace.shard_path("season_data", must_exist=True)
    data = workspace.load(path)
    before = int(data.get(fields.SEASON_COIN) or 0)
    data[fields.SEASON_COIN] = max(before, options.season_coin_value)
    workspace.dump(path, data)
    report["season"] = {"coin": {"before": before, "after": data[fields.SEASON_COIN]}}


def patch_prefs(workspace: SaveWorkspace, catalog, options: UnlockOptions, report: dict) -> None:
    prefs = workspace.load_prefs()
    uid = workspace.uid
    stats: dict[str, Any] = {"unlocks": 0, "skins": 0, "skills": 0, "levels": 0, "pets": 0}

    for key in list(prefs):
        if options.heroes and fields.HERO_UNLOCK.match(key):
            if prefs[key] != fields.OWNED_STRING:
                prefs[key] = fields.OWNED_STRING
                stats["unlocks"] += 1
        elif options.skins and fields.HERO_SKIN.match(key):
            if prefs[key] != fields.OWNED_NUMBER:
                prefs[key] = fields.OWNED_NUMBER
                stats["skins"] += 1
        elif options.skills and fields.HERO_SKILL.match(key):
            if prefs[key] != fields.OWNED_NUMBER:
                prefs[key] = fields.OWNED_NUMBER
                stats["skills"] += 1
        elif options.hero_levels and fields.HERO_LEVEL.match(key):
            if int(prefs[key] or 0) < options.hero_level:
                prefs[key] = options.hero_level
                stats["levels"] += 1
        elif options.pets and fields.PET_UNLOCK.match(key):
            if prefs[key] != fields.OWNED_STRING:
                prefs[key] = fields.OWNED_STRING
                stats["pets"] += 1

    if options.gems:
        for template in (fields.GEMS, fields.LAST_GEMS):
            key = template.format(uid=uid)
            if key in prefs:
                prefs[key] = max(int(prefs[key] or 0), options.gems_value)

    if options.repair_format:
        for template in fields.LEGACY_FLAG_KEYS:
            prefs[template.format(uid=uid)] = 0
        first_active = fields.FIRST_ACTIVE_KEY.format(uid=uid)
        if first_active not in prefs:
            prefs[first_active] = 1
            stats["firstActiveAdded"] = True

    workspace.save_prefs(prefs)
    report["prefs"] = stats


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
PATCHERS = (patch_game, patch_item, patch_evolution, patch_misc, patch_season, patch_prefs)


def apply(
    workspace: SaveWorkspace,
    options: UnlockOptions | None = None,
    *,
    log: Log = lambda message: None,
) -> dict[str, Any]:
    """Patch every file in the local workspace; returns a JSON-friendly report."""
    options = options or UnlockOptions()
    catalog = workspace.catalog()
    report: dict[str, Any] = {
        "uid": workspace.uid,
        "options": asdict(options),
        "catalog": catalog.summary(),
    }
    for patcher in PATCHERS:
        name = patcher.__name__.removeprefix("patch_")
        log(f"patching {name}")
        patcher(workspace, catalog, options, report)
    report["files"] = sorted(path.name for path in workspace.documents.iterdir() if path.is_file())
    return report


def describe_state(workspace: SaveWorkspace) -> dict[str, Any]:
    """Read-only summary of what the save currently has locked."""
    catalog = workspace.catalog()
    game = workspace.load_game()
    prefs = workspace.load_prefs() if workspace.prefs_path.is_file() else {}
    item = workspace.load(workspace.shard_path("item_data", must_exist=True))
    blueprints = item.get(fields.ITEM_BLUEPRINTS) or {}
    evolution = workspace.load(workspace.shard_path("weapon_evolution_data", must_exist=True))
    weapons = evolution.get(fields.EVOLUTION_WEAPONS) or {}
    return {
        "uid": workspace.uid,
        "heroes": {
            "total": len(game.get(fields.GAME_HEROES) or {}),
            "locked": sum(1 for value in (game.get(fields.GAME_HEROES) or {}).values() if value is not True),
        },
        "pets": {
            "total": len(game.get(fields.GAME_PETS) or {}),
            "locked": sum(1 for value in (game.get(fields.GAME_PETS) or {}).values() if value is not True),
        },
        "skins": {
            "entries": sum(len(entries or []) for entries in (game.get(fields.GAME_SKINS) or {}).values()),
            "locked": sum(
                1 for entries in (game.get(fields.GAME_SKINS) or {}).values()
                for entry in entries or [] if entry.get("Value") != fields.OWNED_NUMBER
            ),
            "prefsUnlocked": sum(
                1 for key, value in prefs.items()
                if fields.HERO_SKIN.match(key) and value == fields.OWNED_NUMBER
            ),
        },
        "skills": {
            "entries": sum(len(entries or []) for entries in (game.get(fields.GAME_SKILLS) or {}).values()),
            "locked": sum(
                1 for entries in (game.get(fields.GAME_SKILLS) or {}).values()
                for entry in entries or [] if entry.get("Value") is not True
            ),
        },
        "weapons": {
            "catalog": len(catalog.weapon_ids),
            "blueprints": len(blueprints),
            "unlocked": sum(
                1 for key, value in blueprints.items()
                if key.startswith(fields.BLUEPRINT_WEAPON) and value == fields.BLUEPRINT_UNLOCKED
            ),
            "stillLocked": sorted(
                key for key, value in blueprints.items()
                if key.startswith(fields.BLUEPRINT_WEAPON) and value != fields.BLUEPRINT_UNLOCKED
            )[:20],
            "evolutionEntries": len(weapons),
            "evolved": sum(1 for entry in weapons.values() if int(entry.get("Level") or 0) == 1),
        },
        "killEffects": len(
            (workspace.load(workspace.shard_path("misc_data", must_exist=True)).get(fields.MISC_KILL_EFFECTS) or [])
        ),
        "gems": game.get("gems"),
        "seasonCoin": workspace.load(workspace.shard_path("season_data", must_exist=True)).get(fields.SEASON_COIN),
        "legacyFlags": {
            key.format(uid=workspace.uid): prefs.get(key.format(uid=workspace.uid))
            for key in fields.LEGACY_FLAG_KEYS
        },
        "legacyFiles": [path.name for path in workspace.legacy_files()],
    }
