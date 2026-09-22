"""A synthetic save, built from scratch so the tests ship no real player data."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from sksave import crypto
from sksave.workspace import SaveWorkspace

UID = "123456789"

HAIRLOCK_GAME = {
    "heroUnlock": {"Knight": True, "Rogue": False},
    "petUnlock": {"pet_panda": False, "pet_cat": True},
    "heroLevel": {"Knight": 2, "Rogue": 1},
    "skinLock": {
        "Knight": [{"Name": "Knight_0", "Value": 0}, {"Name": "Knight_1", "Value": 0}],
        "Rogue": [{"Name": "Rogue_0", "Value": 1}],
    },
    "heroSkillUnlock": {
        "Knight": [{"Name": "skill_0", "Value": True}, {"Name": "skill_1", "Value": False}],
        "Rogue": [{"Name": "skill_0", "Value": False}],
    },
    "gems": 120,
    "lastGems": 120,
    "fishChip": 3,
}

ITEM_DATA = {
    "blueprints": {
        "blueprint_weapon_006": "Got",
        "blueprint_m_mech_1": "Researched",
    },
    "mythicWeapons": [{"id": 0, "level": 1}],
    "materials": {"material_wood": 12, "material_iron": 0},
    "seeds": {"plant_banboo_seed": 1},
    "tokenTickets": {"token_a": 5},
    "handStyleIndexList": [1],
}

EVOLUTION = {
    "weapons": {
        "weapon_006": {"Name": "weapon_006", "Level": 0, "CurrentSkinIndex": 0, "UnlockedSkins": []},
    }
}

MISC = {"unlockedKillEffectIds": [3, 7], "somethingElse": 1}
SEASON = {"coin": 100, "gainedCoinToday": 0}
STATISTIC = {
    "handbookWeaponCollectionTrackedLevels": {
        "weapon_006": 3,
        "weapon_184": 3,
        "weapon_shower": 3,
    }
}


def _write_shard(root: Path, dtype: str, value) -> None:
    name = f"{dtype}_{UID}_.data" if dtype not in {"game", "statistic"} else f"{dtype}.data"
    path = root / name
    blob = crypto.encrypt_shard_data(value, dtype, path)
    path.write_bytes(blob.encode("utf-8") if isinstance(blob, str) else blob)


@pytest.fixture()
def save_root(tmp_path: Path) -> Path:
    """A minimal but structurally real save directory."""
    root = tmp_path / "save"
    documents = root / "Documents"
    documents.mkdir(parents=True)
    for namespace in ("hot_update", "Config"):
        (documents / namespace).mkdir()

    _write_shard(documents, "game", HAIRLOCK_GAME)
    _write_shard(documents, "item_data", ITEM_DATA)
    _write_shard(documents, "weapon_evolution_data", EVOLUTION)
    _write_shard(documents, "misc_data", MISC)
    _write_shard(documents, "season_data", SEASON)
    _write_shard(documents, "statistic", STATISTIC)

    prefs = {
        f"{UID}_c0_unlock": "False",
        f"{UID}_c1_unlock": "True",
        f"{UID}_c0_skin0": 0,
        f"{UID}_c0_skin1": 0,
        f"{UID}_c1_skin0": 1,
        f"{UID}_c0_level": 1,
        f"{UID}_c1_level": 2,
        f"{UID}_c_Knight_skill_1_unlock": 0,
        f"{UID}_p0_unlock": "False",
        f"{UID}_gems": 120,
        f"{UID}_last_gems": 120,
        f"OpenRijTest_{UID}": 1,
        f"OpenNewtonJsonTest_{UID}": 1,
        "unrelatedKey": "keep me",
    }
    (root / "prefs.plist").write_bytes(plistlib.dumps(prefs, fmt=plistlib.FMT_BINARY))

    # files the game writes in its unsupported format
    (documents / f"item_data_{UID}_.data.new").write_bytes(b"\x00" * 32)
    (documents / f"battles_{UID}_.data.rij").write_bytes(b"\x01" * 16)
    return root


@pytest.fixture()
def workspace(save_root: Path) -> SaveWorkspace:
    return SaveWorkspace(save_root)
