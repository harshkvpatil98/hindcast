"""The child interpreter one replay runs in (spec §8.4; M8).

A module rather than a generated script so the replayed code path is the same
code the study runs, imported the same way. ``python -m
cascade.trace.replay_child <run_id>`` prints one JSON line and exits.

The parent sets ``PYTHONHASHSEED`` before spawning this, which is the entire
reason the child exists: a set iterated for a tie-break is stable within a
process and diverges across one.
<<<<<<< HEAD
=======

Exit codes are the project's (``cascade/version.py``). A replay that needs a
model recording this machine does not hold exits with the cache-miss code and
one JSON line naming the key, so the parent files the run as *unreplayable
here* rather than as a divergence (M17): the recording's absence says nothing
about determinism.
>>>>>>> source-clean/main
"""

from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m cascade.trace.replay_child <run_id>", file=sys.stderr)
        return 2

    from cascade.config import Settings
<<<<<<< HEAD
    from cascade.trace.replay import load_replay_targets, replay_in_process

    run_id = argv[1]
    settings = Settings()
    targets = [
        target
        for target in load_replay_targets(settings, limit=1_000_000)
        if target.run_id == run_id
    ]
    if not targets:
        print(f"no run {run_id!r} in the runs table", file=sys.stderr)
        return 3

    print(json.dumps(replay_in_process(settings, targets[0])))
=======
    from cascade.llm.types import CacheMiss
    from cascade.trace.replay import load_replay_targets, replay_in_process
    from cascade.version import EXIT_CACHE_MISS, EXIT_PRECONDITION

    run_id = argv[1]
    settings = Settings()
    # Exactly the one run, by key: loading every stored run to find it would
    # read every event in the database for each replay.
    targets = load_replay_targets(settings, limit=1, run_id=run_id)
    if not targets:
        print(f"no run {run_id!r} in the runs table", file=sys.stderr)
        return EXIT_PRECONDITION

    try:
        print(json.dumps(replay_in_process(settings, targets[0])))
    except CacheMiss as exc:
        print(json.dumps({"run_id": run_id, "error": str(exc)}))
        return EXIT_CACHE_MISS
>>>>>>> source-clean/main
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    sys.exit(main(sys.argv))
