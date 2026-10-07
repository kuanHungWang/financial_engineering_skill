"""--set overrides: mapping keys, list indices, and input errors (exit 2, not a crash).

Run:  python -m pytest tests -q
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pricer import OverrideError, apply_overrides  # noqa: E402

EX = ROOT / "examples"
NOTE = EX / "autocallable_note.yaml"


def note():
    return yaml.safe_load(NOTE.read_text())


@pytest.mark.parametrize("path", ["trade.legs.0.rate", "trade.legs[0].rate"])
def test_numeric_segment_indexes_list(path):
    doc = apply_overrides(note(), [f"{path}=0.09"])
    assert doc["trade"]["legs"][0]["rate"] == 0.09
    assert "legs[0]" not in doc["trade"]


def test_mapping_keys_are_still_created():
    doc = apply_overrides(note(), ["pricing.new_section.num_paths=50000"])
    assert doc["pricing"]["new_section"]["num_paths"] == 50000


def test_whole_list_element_can_be_replaced():
    doc = apply_overrides(note(), ["trade.legs.1={id: principal, type: fixed_amount, amount: 5, payment_date: maturity}"])
    assert doc["trade"]["legs"][1]["amount"] == 5


@pytest.mark.parametrize("expr, fragment", [
    ("trade.legs.9.rate=0.09", "index 9 out of range: trade.legs has 3 item(s)"),
    ("trade.legs.first.rate=0.09", "is a list; index it with a number"),
    ("trade.legs.0.rate.x=1", "it is a float"),
    ("trade..rate=1", "empty segment"),
    ("trade.legs.\u00b2.rate=1", "is a list; index it with a number"),
    ("trade.legs[\u00b2].rate=1", "list indices must be non-negative integers"),
    ("trade.legs[-1].rate=1", "list indices must be non-negative integers"),
    ("trade.legs[x].rate=1", "list indices must be non-negative integers"),
    ("pricing.num_paths=[1", "not valid YAML"),
    ("pricing.num_paths", "expects PATH=VALUE"),
])
def test_bad_paths_raise_override_error(expr, fragment):
    with pytest.raises(OverrideError) as e:
        apply_overrides(note(), [expr])
    assert fragment in str(e.value)


def cli(*args):
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "price.py"), *args],
                       capture_output=True, text=True, cwd=ROOT)
    return r.returncode, json.loads(r.stdout)


def test_cli_list_index_override_validates():
    code, out = cli("validate", str(NOTE), "--set", "trade.legs.0.rate=0.09")
    assert code == 0 and out["ok"]


def test_cli_out_of_range_index_is_input_error():
    code, out = cli("validate", str(NOTE), "--set", "trade.legs.5.rate=0.09")
    assert code == 2
    assert out["ok"] is False
    [err] = out["errors"]
    assert err["path"] == "trade.legs.5.rate"
    assert "out of range" in err["message"]
