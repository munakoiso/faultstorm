"""Final reads must be validated independently for every configured node."""

import json

from faultstorm.checker import check_consistency


def _check(tmp_path, events):
    operations_log = tmp_path / "operations.log"
    with operations_log.open("w") as stream:
        for event in events:
            stream.write(json.dumps(event) + "\n")
    return check_consistency(str(operations_log), ["db1", "db2"])


def test_last_successful_read_per_node_detects_loss(tmp_path):
    result = _check(tmp_path, [
        {"type": "invoke", "action": "add", "value": 1},
        {"type": "ok", "action": "add", "value": 1},
        {"type": "ok", "action": "read", "node": "db1", "value": [1]},
        {"type": "ok", "action": "read", "node": "db2", "value": [1]},
        {"type": "ok", "action": "read", "node": "db1", "value": []},
    ])

    assert not result.valid
    assert result.lost == {1}
    assert "Final reads differ" in result.errors


def test_indeterminate_write_on_only_one_node_is_divergence(tmp_path):
    result = _check(tmp_path, [
        {"type": "invoke", "action": "add", "value": 1},
        {"type": "ok", "action": "add", "value": 1},
        {"type": "invoke", "action": "add", "value": 2},
        {"type": "ok", "action": "read", "node": "db1", "value": [1, 2]},
        {"type": "ok", "action": "read", "node": "db2", "value": [1]},
    ])

    assert not result.valid
    assert not result.lost
    assert not result.unexpected
    assert result.recovered == {2}
    assert "Final reads differ" in result.errors


def test_missing_node_read_fails_validation(tmp_path):
    result = _check(tmp_path, [
        {"type": "ok", "action": "read", "node": "db1", "value": []},
        {"type": "fail", "action": "read", "node": "db2", "error": "timeout"},
    ])

    assert not result.valid
    assert result.errors == "No successful reads from: db2"
