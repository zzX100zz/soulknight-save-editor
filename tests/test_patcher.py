"""The unlock rules, checked on a synthetic save."""

from __future__ import annotations

from sksave import fields
from sksave.patcher import UnlockOptions, apply, describe_state


def test_apply_unlocks_everything(workspace):
    report = apply(workspace, UnlockOptions())
    state = describe_state(workspace)

    assert state["heroes"]["locked"] == 0
    assert state["pets"]["locked"] == 0
    assert state["skins"]["locked"] == 0
    assert state["skills"]["locked"] == 0
    assert state["weapons"]["stillLocked"] == []
    assert state["weapons"]["unlocked"] >= 3
    assert state["killEffects"] >= 64
    assert state["gems"] >= fields.GEM_MAX
    assert state["seasonCoin"] == fields.SEASON_COIN_MAX
    assert report["game"]["heroLevelsRaised"] == 2


def test_blueprint_state_is_researched(workspace):
    apply(workspace, UnlockOptions())
    item = workspace.load(workspace.shard_path("item_data", must_exist=True))
    blueprints = item[fields.ITEM_BLUEPRINTS]
    weapons = {k: v for k, v in blueprints.items() if k.startswith(fields.BLUEPRINT_WEAPON)}
    assert weapons, "expected weapon blueprints to exist"
    assert set(weapons.values()) == {fields.BLUEPRINT_UNLOCKED}
    # non-weapon blueprints keep whatever state they had
    assert blueprints["blueprint_m_mech_1"] == "Researched"


def test_prefs_types_are_exact(workspace):
    apply(workspace, UnlockOptions())
    prefs = workspace.load_prefs()
    assert prefs[f"{workspace.uid}_c0_unlock"] == "True"       # string, not bool
    assert prefs[f"{workspace.uid}_c0_skin0"] == 1             # int, not string
    assert prefs[f"{workspace.uid}_c_Knight_skill_1_unlock"] == 1
    assert prefs[f"{workspace.uid}_p0_unlock"] == "True"
    assert prefs[f"{workspace.uid}_c0_level"] >= fields.HERO_LEVEL_MAX
    assert prefs["unrelatedKey"] == "keep me"


def test_format_switches_are_cleared(workspace):
    apply(workspace, UnlockOptions())
    prefs = workspace.load_prefs()
    for template in fields.LEGACY_FLAG_KEYS:
        assert prefs[template.format(uid=workspace.uid)] == 0
    assert prefs[fields.FIRST_ACTIVE_KEY.format(uid=workspace.uid)] == 1


def test_quantities_raised(workspace):
    apply(workspace, UnlockOptions())
    item = workspace.load(workspace.shard_path("item_data", must_exist=True))
    assert item["materials"]["material_wood"] == fields.MAX_QUANTITY
    assert item["seeds"]["plant_banboo_seed"] == fields.MAX_QUANTITY
    assert item["tokenTickets"]["token_a"] == fields.MAX_QUANTITY
    assert item["handStyleIndexList"] == fields.HAND_STYLES


def test_evolution_entries(workspace):
    apply(workspace, UnlockOptions())
    data = workspace.load(workspace.shard_path("weapon_evolution_data", must_exist=True))
    weapons = data[fields.EVOLUTION_WEAPONS]
    assert weapons, "expected evolution entries"
    assert all(int(entry["Level"]) == 1 for entry in weapons.values())
    assert all(entry["UnlockedSkins"] for entry in weapons.values())


def test_idempotent(workspace):
    apply(workspace, UnlockOptions())
    first = {
        name: path.read_bytes() for name, path in workspace.all_shards().items()
    }
    apply(workspace, UnlockOptions())
    second = {name: path.read_bytes() for name, path in workspace.all_shards().items()}
    assert first == second


def test_currency_never_goes_down(workspace):
    game = workspace.load_game()
    game["gems"] = 9_000_000_000
    workspace.save_game(game)
    apply(workspace, UnlockOptions())
    assert workspace.load_game()["gems"] == 9_000_000_000


def test_options_can_skip_categories(workspace):
    options = UnlockOptions(gems=False, season_coin=False, kill_effects=False)
    apply(workspace, options)
    state = describe_state(workspace)
    assert state["gems"] == 120
    assert state["seasonCoin"] == 100
    assert state["killEffects"] == 2


def test_legacy_files_are_listed(workspace):
    names = {path.name for path in workspace.legacy_files()}
    assert f"item_data_{workspace.uid}_.data.new" in names
    assert f"battles_{workspace.uid}_.data.rij" in names
