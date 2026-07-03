# FaultStorm

Fault-injection testing framework for distributed databases — verifies data consistency under network partitions, process crashes, and failovers.

## Overview

FaultStorm runs a **write + fault + read** cycle against a cluster of Docker containers:

1. **Write phase** — parallel writer threads insert sequential values into a set table.
2. **Fault phase** — runs concurrently with writes; the engine randomly injects faults, waits, heals, and repeats.
3. **Read phase** — reads the final state from every node.
4. **Consistency check** — compares attempted, confirmed, and read values to detect data loss or corruption.

The framework is **database-agnostic**: implement the `DatabaseClient` abstract class for your DBMS and register any custom `FaultAction` subclasses in a `FaultRegistry`.

## Architecture

```
TestRunner          ← top-level API: setup → write+faults → read → check
├── LoadGenerator   ← parallel writes/reads with app-side timeouts
├── FaultEngine     ← random or replay fault injection
│   └── FaultRegistry + FaultAction subclasses
├── NetworkLatencyManager  ← static tc/netem rules for DC latency emulation
└── ConsistencyChecker     ← set-based lost/unexpected/recovered analysis
```

### Key components

| Component | Description |
|---|---|
| `TestConfig` | Dataclass with all timing, node lists, and fault settings |
| `DatabaseClient` | ABC: `setup()`, `add()`, `read()`, `is_definite_failure()` |
| `FaultAction` | ABC: `execute()`, `heal()`, `serialize()`, `deserialize()` |
| `FaultRegistry` | Maps action `name` → class; extensible via `register()` |
| `FaultEngine` | Random mode (random faults with heal cycles) and replay mode |
| `LoadGenerator` | Threaded writers with JSON operations log |
| `CheckResult` | Result dataclass: `valid`, `lost`, `unexpected`, `recovered` |

## Fault engine cycle

Each cycle of the random-mode engine:

1. Inject `parallel_faults_count` faults (default: 2) with random waits between them.
2. Wait `fault_active_duration` seconds.
3. Heal all active faults in random order, with random waits between healable ones.
4. Wait `fault_pause_duration` seconds.
5. Repeat until the write phase ends.

### Complex (multi-fault) injection

When `complex_faults_enabled` is `True` (the default), each injection step randomly chooses between:

- **Single fault** — one random action from all enabled types (50% chance).
- **Complex fault** — 1–3 `host_targetable` actions fired on a **single** random DB node with random waits (0–`complex_fault_max_wait` seconds) between them (50% chance).

This simulates realistic failure scenarios where a node experiences multiple overlapping issues (e.g. a network partition + process freeze on the same host).

Only actions with `host_targetable = True` participate in combos (currently `partition_random_node`, `partition_random_subnet`, `freeze_processes`).
If no host-targetable types are in the configured `fault_types`, the engine falls back to single-fault mode.

## Built-in fault actions

| Action | Description | Healable |
|---|---|---|
| `wait` | Sleep for N seconds (interruptible) | No |
| `kill` | Kill a process on a random DB node | No |
| `partition_random_halves` | Split all nodes into two isolated groups | Yes |
| `partition_majorities_ring` | Each node sees a majority but groups differ | Yes |
| `partition_random_node` | Isolate a single node from all others | Yes |
| `partition_random_subnet` | Directional iptables filter on one node | Yes |
| `partition_random_dc` | Isolate an entire datacenter | Yes |
| `freeze_processes` | SIGSTOP/SIGCONT cycle on one node | Yes |
| `freeze_processes_group` | SIGSTOP/SIGCONT on an entire node group | Yes |

### Action flags

- **`healable`** — the engine writes `+`/`-` prefixes in the scenario log and calls `heal()` during the heal phase with random waits between healable actions.
- **`destructive`** — counted against `max_destructive_actions`; once the limit is reached, destructive types are excluded.
- **`host_targetable`** — eligible for complex (multi-fault) combos on a single DB node.

## Adding a custom action

```python
from faultstorm.faults.actions import FaultAction

class MyAction(FaultAction):
    name = "my_action"
    healable = True

    def execute(self, stop_event=None):
        ...  # inject the fault

    def heal(self):
        ...  # reverse the fault

    def serialize(self):
        return f"{self.ordinal} {self.some_param}"

    @classmethod
    def deserialize(cls, params, db_nodes, extra_nodes, **kwargs):
        parts = params.split()
        ordinal = int(parts[0])
        some_param = parts[1]
        action = cls(db_nodes, extra_nodes, ordinal, **kwargs)
        action.some_param = some_param
        return action
```

Register it:

```python
from faultstorm.faults.actions import create_default_registry

registry = create_default_registry()
registry.register(MyAction)
```

## Scenario log format

The engine writes a human-readable, replayable log:

```
# FaultStorm scenario log
[2026-06-23T14:00:10.123] kill 1 postgres postgresql1
[2026-06-23T14:00:10.456] +partition_random_node 2 zookeeper2
[2026-06-23T14:00:10.789] wait 3 60
[2026-06-23T14:00:70.012] -partition_random_node 2 zookeeper2
```

- `+` prefix = healable fault enabled
- `-` prefix = healable fault healed
- No prefix = fire-and-forget action

Replay with `FaultEngine.run_replay(path)`.

## Running tests

The test suite uses [Behave](https://behave.readthedocs.io/) and Docker Compose:

```bash
make test-build   # build Docker images
make test          # run all behave tests
```

## Installation

```bash
pip install -e .                 # core (no DB driver)
pip install -e ".[postgres]"     # with psycopg2 for PostgreSQL
pip install -e ".[dev]"          # dev tools (mypy, flake8, etc.)
```
