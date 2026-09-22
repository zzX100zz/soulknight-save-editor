"""Field names, value shapes and key patterns of the Soul Knight iOS save.

Everything in here was verified against a live 8.5.1 save (see docs/SAVE_FORMAT.md).
The important part is that the *type* of a value matters: the game compares an
NSString for hero/pet unlocks, an NSNumber for skins and skills, and it silently
treats a mismatch as "not owned".
"""

from __future__ import annotations

import re
from typing import Iterable

# --------------------------------------------------------------------------- #
# app / container
# --------------------------------------------------------------------------- #
BUNDLE_ID = "com.ChillyRoom.DungeonShooter"
PREFS_NAME = "com.ChillyRoom.DungeonShooter.plist"      # iOS (binary plist)
ANDROID_PREFS_NAME = "com.ChillyRoom.DungeonShooter.v2.playerprefs.xml"

DOCUMENTS = "Documents"
GAME_DATA = "game.data"

# split save files that carry the UID in their name: <type>_<uid>_.data
UID_SHARD = "{dtype}_{uid}_.data"

# files the game writes once it has switched to its new (Rijndael) format; the
# editor cannot read them, so they are moved aside to make the game read the
# classic shards again
LEGACY_FORMAT_SUFFIXES = (".data.new", ".data.rij")

# PlayerPrefs switches that select the save format.  Both must be 0 for the game
# to use the classic *.data shards.  The server turns them back on during a
# session, so they are re-cleared on every run.
LEGACY_FLAG_KEYS = ("OpenRijTest_{uid}", "OpenNewtonJsonTest_{uid}")
# without this mirror the game can treat the save as a fresh account
FIRST_ACTIVE_KEY = "{uid}_first_active"

# --------------------------------------------------------------------------- #
# PlayerPrefs key patterns (iOS plist uses the UID as a prefix)
# --------------------------------------------------------------------------- #
HERO_UNLOCK = re.compile(r"^(?P<uid>\d+)_c(?P<hero>\d+)_unlock$")
HERO_SKIN = re.compile(r"^(?P<uid>\d+)_c(?P<hero>\d+)_skin(?P<skin>\d+)$")
HERO_LEVEL = re.compile(r"^(?P<uid>\d+)_c(?P<hero>\d+)_level$")
HERO_SKILL = re.compile(r"^(?P<uid>\d+)_c_(?P<hero>[A-Za-z0-9]+)_skill_(?P<slot>\d+)_unlock$")
PET_UNLOCK = re.compile(r"^(?P<uid>\d+)_p(?P<pet>\d+)_unlock$")
GEMS = "{uid}_gems"
LAST_GEMS = "{uid}_last_gems"

# values that mean "owned" (types matter, see the module docstring)
OWNED_STRING = "True"
OWNED_NUMBER = 1

# --------------------------------------------------------------------------- #
# game.data / item_data field names
# --------------------------------------------------------------------------- #
GAME_SKINS = "skinLock"
GAME_SKILLS = "heroSkillUnlock"
GAME_HEROES = "heroUnlock"
GAME_PETS = "petUnlock"
GAME_HERO_LEVELS = "heroLevel"
GAME_FISH_CHIP = "fishChip"

ITEM_BLUEPRINTS = "blueprints"
ITEM_MYTHIC = "mythicWeapons"
ITEM_HAND_STYLES = "handStyleIndexList"
ITEM_QUANTITY_FIELDS = ("materials", "seeds", "tokenTickets")

MISC_KILL_EFFECTS = "unlockedKillEffectIds"
SEASON_COIN = "coin"
EVOLUTION_WEAPONS = "weapons"

# The weapon hall shows a weapon as unlocked only when its blueprint is in the
# "Researched" state; "Got" just means the blueprint dropped (verified against a
# full-unlock reference save, where all 58 weapon blueprints are "Researched").
BLUEPRINT_UNLOCKED = "Researched"
BLUEPRINT_DROPPED = "Got"

BLUEPRINT_WEAPON = "blueprint_weapon_"
BLUEPRINT_EVOLUTION = "blueprint_evolution_weapon_"
BLUEPRINT_TRANSFORM = "blueprint_transform_weapon_"

# --------------------------------------------------------------------------- #
# caps
# --------------------------------------------------------------------------- #
HERO_LEVEL_MAX = 8            # the game itself levels heroes to 8
MAX_QUANTITY = 99_999
MYTHIC_LEVEL_MAX = 3
MYTHIC_ID_RANGE = range(0, 28)
KILL_EFFECT_ID_RANGE = range(0, 64)     # over-listing is harmless: the UI enumerates its own config
SEASON_COIN_MAX = 9_999_999
GEM_MAX = 2_000_000_000                 # int32 is 2,147,483,647 - keep headroom
HAND_STYLES = [5, 6]

# weapon ids without a numeric part also exist (`weapon_shower`, `weapon_joker`);
# the numeric id space is filled up to this bound as a safety net, because the
# newest weapons are not part of every save's own handbook table.
NUMERIC_WEAPON_RANGE = range(0, 601)


def weapon_ids_from_handbook(handbook: dict) -> list[str]:
    """Every ``weapon_<id>`` key the save itself enumerates."""
    return sorted(key for key in handbook if key.startswith("weapon_"))


def weapon_key_forms(tail: str) -> list[str]:
    """Both spellings the game uses for a weapon id (``_006`` and ``_6``)."""
    if tail.isdigit():
        return sorted({tail, f"{int(tail):03d}"})
    return [tail]


def is_weapon_id(value: str) -> bool:
    return bool(re.fullmatch(r"weapon_[0-9A-Za-z]+", value))
