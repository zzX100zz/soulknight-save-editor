"""Save format: name -> cipher mapping and byte-accurate round trips."""

from __future__ import annotations

import json

import pytest

from sksave import crypto


@pytest.mark.parametrize(
    "name,expected",
    [
        ("game.data", "game"),
        ("item_data_123456789_.data", "item_data"),
        ("item_data.data", "item_data"),
        ("mall_reload_data_LOCAL.data", "mall_reload_data"),
        ("statistic_123456789_.data", "statistic"),
        ("weapon_evolution_data_123456789_.data", "weapon_evolution_data"),
        ("misc_data_123456789_.data", "misc_data"),
    ],
)
def test_data_type_from_name(name, expected):
    assert crypto.data_type_from_name(name) == expected


@pytest.mark.parametrize("dtype", ["game", "item_data", "statistic", "misc_data", "season_data"])
def test_round_trip(tmp_path, dtype):
    payload = {"text": "控制字符\tinside\na string", "numbers": [1, 2, 3], "nested": {"a": True}}
    path = tmp_path / (f"{dtype}_{123456789}_.data" if dtype not in {"game"} else "game.data")
    blob = crypto.encrypt_shard_data(payload, dtype, path)
    path.write_bytes(blob.encode("utf-8") if isinstance(blob, str) else blob)
    restored, algorithm = crypto.decrypt_file(path)
    assert restored == payload
    assert algorithm in {"XOR", "DES-iambo", "DES-crst1"}


def test_unknown_type_is_refused(tmp_path):
    with pytest.raises(ValueError):
        crypto.encrypt_shard_data({"a": 1}, "mystery_shard", tmp_path / "mystery_shard_1_.data")


def test_xor_is_symmetric():
    blob = b"hello-save-data"
    assert crypto.xor_bytes(crypto.xor_bytes(blob)) == blob
