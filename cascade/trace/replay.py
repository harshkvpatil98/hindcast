"""Re-running a stored run and proving it came back identical (spec §8.4; M8).

M8's first criterion is a **byte-identical event-log hash across processes**
over 25 runs. Two halves of that matter separately.

*Byte-identical* is asserted against the hash the original run stored in
``runs.event_log_hash``, not against a second in-process run. A hash compared
only with itself proves the hash function is a function.

*Across processes* is the half that finds real defects. Within one process a
dependency on dict insertion order, on ``id()``, or on a module-level cache is
perfectly stable; ``PYTHONHASHSEED`` differs between processes, so a set
iterated for a tie-break diverges there and nowhere else. Each replay
therefore runs in its own interpreter, under a hash seed chosen to differ from
the parent's.

When a run does diverge, the answer "the hashes differ" is useless. §8.4's
rule is that the bisect names the first divergent field, so a mismatch is
localised to a step by comparing the stored per-step ``state_hash`` sequence
against the replayed one, and the first index that differs is reported with
both values.

*What the replay is compared against* is the stored **events**, not the
stored digest. ``runs.event_log_hash`` is a derived column written when the
run was stored, and the domain it is computed over has moved once already:
M16 dropped ``cache_hit`` from ``DecisionEvent.canonical()`` and did not
re-hash the 2,480 runs stored before it, so every one of them compared as a
divergence against a digest from an older definition (found at M17). The
target's hash is therefore recomputed here from the stored rows under the
current definition; the cached digest is carried beside it and reported as
*stale* when it differs, and ``cascade trace rehash`` refreshes it. The events
themselves are the record and are never touched (invariant 6).

Two outcomes that are not a pass are also not a divergence, and the report
keeps them apart: a run whose model recordings are not on this machine cannot
be replayed here (the child exits with the cache-miss code and the outcome is
*unreplayable*), and a child that died for a harness reason has *failed*.
Both are "unverified", which is the honest word for them.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from cascade.config import Settings
from cascade.trace.events import DecisionEvent, EventRef, StepRecord, event_log_hash
from cascade.version import EXIT_CACHE_MISS

__all__ = [
    "RehashItem",
    "ReplayOutcome",
    "ReplayReport",
    "ReplayTarget",
    "apply_rehash",
    "load_replay_targets",
    "plan_rehash",
    "replay_in_process",
    "verify_replays",
]

Role = Literal["admin", "sim", "eval"]

# The child's PYTHONHASHSEED. Any fixed value differs from the parent's random
# one, which is the whole point; fixing it keeps the *child* reproducible so a
# divergence is a property of the code rather than of the run that found it.
CHILD_HASH_SEED = "12345"


@dataclass(frozen=True, slots=True)
class ReplayTarget:
    """One stored run, and the hash it must reproduce.

    ``event_log_hash`` is the stored events and step records hashed under the
    *current* domain -- what a correct replay reproduces. ``stored_digest`` is
    what ``runs.event_log_hash`` holds; when the two differ the cached column
    predates a change to the hash domain and ``cascade trace rehash`` refreshes
    it. ``None`` only for targets built by hand without a stored digest.
    """

    run_id: str
    scenario_id: str
    config_id: str
    replicate: int
    policy: str
    event_log_hash: str
    state_hashes: tuple[str, ...]
    outcome_score: float
    decisions: int
    stored_digest: str | None = None

    @property
    def digest_stale(self) -> bool:
        """Whether the cached digest predates the current hash domain."""
        return self.stored_digest is not None and self.stored_digest != self.event_log_hash


ReplayKind = Literal["reproduced", "unreplayable", "failed", "diverged"]


@dataclass(frozen=True, slots=True)
class ReplayOutcome:
    """What one replay produced, and where it first differed if it did."""

    run_id: str
    expected_hash: str
    actual_hash: str | None
    first_divergent_step: int | None = None
    expected_state_hash: str | None = None
    actual_state_hash: str | None = None
    error: str | None = None
    # The child could not replay this run *here*: a model recording it needs
    # is not in this machine's cache. Says nothing about determinism.
    unreplayable: bool = False
    # The run's cached digest predates the current hash domain (see the module
    # docstring). Reported, never counted against the replay.
    digest_stale: bool = False

    @property
    def matched(self) -> bool:
        return self.error is None and self.actual_hash == self.expected_hash

    @property
    def kind(self) -> ReplayKind:
        """One of four words, because three kinds of "not a pass" differ in remedy.

        *diverged* is the only one that is a determinism defect, and §8.4's
        rule applies to it alone: fix the source. *unreplayable* wants the
        recordings; *failed* wants the harness looked at.
        """
        if self.matched:
            return "reproduced"
        if self.unreplayable:
            return "unreplayable"
        if self.error is not None:
            return "failed"
        return "diverged"


@dataclass
class ReplayReport:
    """The M8 criterion, measured."""

    outcomes: list[ReplayOutcome] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def attempted(self) -> int:
        return len(self.outcomes)

    @property
    def matched(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.matched)

    @property
    def diverged(self) -> tuple[ReplayOutcome, ...]:
        """Every outcome that is not a pass, in target order."""
        return tuple(outcome for outcome in self.outcomes if not outcome.matched)

    @property
    def nondeterministic(self) -> tuple[ReplayOutcome, ...]:
        """The outcomes that are a determinism defect: a replay that ran and differed."""
        return tuple(outcome for outcome in self.outcomes if outcome.kind == "diverged")

    @property
    def unverified(self) -> tuple[ReplayOutcome, ...]:
        """The outcomes nothing can be concluded from: no recording here, or a harness failure."""
        return tuple(
            outcome for outcome in self.outcomes if outcome.kind in ("unreplayable", "failed")
        )

    @property
    def stale_digests(self) -> int:
        """How many targets carried a cached digest from an older hash domain."""
        return sum(1 for outcome in self.outcomes if outcome.digest_stale)

    @property
    def ok(self) -> bool:
        return bool(self.outcomes) and not self.diverged


def _connect(settings: Settings, role: Role) -> Any:
    import psycopg

    return psycopg.connect(settings.database_url(role), connect_timeout=30)


def load_replay_targets(
    settings: Settings,
    *,
    limit: int,
    config_id: str | None = None,
    role: Role = "eval",
    policy: str | None = None,
    run_id: str | None = None,
) -> tuple[ReplayTarget, ...]:
    """The runs to replay, chosen deterministically, each with the hash it must reproduce.

    Ordered by ``run_id`` and taken from the front rather than sampled: the
    criterion is "25 runs replay identically", and a random 25 would make a
    failure depend on which 25 the command happened to draw. The same 25 every
    time is what makes a green result mean the same thing twice.

    The hash to reproduce is computed from the stored events and step records
    under the current hash domain, through the same ``canonical()`` the kernel
    uses -- never read from ``runs.event_log_hash``, which is a cached digest
    that can predate a change to that domain (the module docstring has the
    history). The cached digest rides along so the report can say it is stale.

    ``policy`` narrows to runs made by one decider -- a machine without the
    recordings a model-backed run needs can still verify the stand-in runs --
    and ``run_id`` is how the child process asks for exactly one run.
    """
    clauses: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if config_id:
        clauses.append("config_id = %(config_id)s")
        params["config_id"] = config_id
    if policy:
        clauses.append("policy = %(policy)s")
        params["policy"] = policy
    if run_id:
        clauses.append("run_id = %(run_id)s")
        params["run_id"] = run_id
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect(settings, role) as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT run_id, scenario_id, config_id, replicate, policy,
                   event_log_hash, outcome_score, decisions
            FROM runs {where}
            ORDER BY run_id
            LIMIT %(limit)s
            """,  # noqa: S608 -- `where` is assembled from literals above, values are bound
            params,
        )
        rows = cur.fetchall()

        targets: list[ReplayTarget] = []
        for row_id, scenario_id, cfg, replicate, row_policy, digest, outcome, decisions in rows:
            events, steps = _stored_log(cur, str(row_id))
            targets.append(
                ReplayTarget(
                    run_id=str(row_id),
                    scenario_id=str(scenario_id),
                    config_id=str(cfg),
                    replicate=int(replicate),
                    policy=str(row_policy),
                    event_log_hash=event_log_hash(events, steps),
                    state_hashes=tuple(step.state_hash for step in steps),
                    outcome_score=float(outcome),
                    decisions=int(decisions),
                    stored_digest=str(digest),
                )
            )
    return tuple(targets)


def _stored_log(cur: Any, run_id: str) -> tuple[tuple[DecisionEvent, ...], tuple[StepRecord, ...]]:
    """One run's events and step records, as the kernel's own types, in key order.

    Rebuilt through the Pydantic models rather than hashed from the raw rows,
    so the digest comes from the one definition of ``canonical()`` the kernel
    uses -- and a stored row that no longer validates is an error here rather
    than a silently different hash. Ordered by key, never by physical order
    (spec §8.1).
    """
    cur.execute(
        """
        SELECT step, seq, actor_id, obs_hash, action, caused_by, factor_delta,
               cache_hit, tokens_in, tokens_out, latency_ms, coercion
        FROM events WHERE run_id = %s ORDER BY step, seq
        """,
        (run_id,),
    )
    events = tuple(
        DecisionEvent(
            run_id=run_id,
            step=int(step),
            seq=int(seq),
            actor_id=str(actor_id),
            obs_hash=bytes(obs_hash),
            action=dict(action),
            caused_by=tuple(EventRef(**ref) for ref in caused_by),
            factor_delta={key: float(value) for key, value in sorted(dict(factor_delta).items())},
            cache_hit=bool(cache_hit),
            tokens_in=int(tokens_in),
            tokens_out=int(tokens_out),
            latency_ms=None if latency_ms is None else int(latency_ms),
            coercion=None if coercion is None else str(coercion),
        )
        for (
            step,
            seq,
            actor_id,
            obs_hash,
            action,
            caused_by,
            factor_delta,
            cache_hit,
            tokens_in,
            tokens_out,
            latency_ms,
            coercion,
        ) in cur.fetchall()
    )
    cur.execute(
        """
        SELECT step, exogenous_delta, arrivals, contest_delta, active, eligible,
               rng_counter, state_hash, absorbed
        FROM run_steps WHERE run_id = %s ORDER BY step
        """,
        (run_id,),
    )
    steps = tuple(
        StepRecord(
            run_id=run_id,
            step=int(step),
            exogenous_delta={k: float(v) for k, v in sorted(dict(exogenous).items())},
            arrivals={k: float(v) for k, v in sorted(dict(arrivals).items())},
            contest_delta={k: float(v) for k, v in sorted(dict(contest).items())},
            active=tuple(str(actor) for actor in (active or ())),
            eligible=int(eligible),
            rng_counter=int(rng_counter),
            state_hash=str(state_hash),
            absorbed=tuple(str(factor) for factor in (absorbed or ())),
        )
        for (
            step,
            exogenous,
            arrivals,
            contest,
            active,
            eligible,
            rng_counter,
            state_hash,
            absorbed,
        ) in cur.fetchall()
    )
    return events, steps


def replay_in_process(settings: Settings, target: ReplayTarget) -> dict[str, Any]:
    """Re-simulate one run here and return its hashes. Writes nothing.

    The replay must not touch the event log: invariant 6 makes it append-only,
    and a verification pass that inserted its own copy would make the M6 event
    count drift every time someone checked the M8 criterion.
    """
    from cascade.aperture.policy import derive_policies
    from cascade.cli import _graph_for
    from cascade.ledger.store import load_scenarios
    from cascade.sim.kernel import Loom, RunSpec, mechanics_from
    from cascade.sim.policies import HeuristicPolicy

    cell = load_settings_for(target.config_id)
    graph = _graph_for(cell, target.scenario_id)
    if graph is None:
        raise LookupError(
            f"scenario {target.scenario_id!r} has no compiled graph, so the run that "
            "produced this hash cannot be reconstructed; run `cascade compile build`"
        )
    scenarios = {item.scenario_id: item for item in load_scenarios(cell, role="admin")}
    if target.scenario_id not in scenarios:
        raise LookupError(f"scenario {target.scenario_id!r} is not in the registry")

    policies = derive_policies(graph, cell.aperture, asymmetry=cell.flags.information_asymmetry)
    if target.policy == "heuristic":
        mechanics = mechanics_from(graph)
        decider: Any = HeuristicPolicy(
            utility={actor: dict(mechanics[actor].utility) for actor in sorted(mechanics)}
        )
    else:
        from cascade.cli import _agent_policy

        decider = _agent_policy(cell, [(scenarios[target.scenario_id], graph)])

    loom = Loom(settings=cell, graph=graph, policies=policies, decider=decider)
    result = loom.run(
        RunSpec(
            run_id=target.run_id,
            scenario_id=target.scenario_id,
            config_id=target.config_id,
            replicate=target.replicate,
            policy=target.policy,
        )
    )
    return {
        "run_id": target.run_id,
        "event_log_hash": result.event_log_hash,
        "state_hashes": [record.state_hash for record in result.step_records],
        "outcome_score": result.outcome_score,
        "decisions": result.decisions,
    }


def load_settings_for(config_id: str) -> Settings:
    """Load the settings a run was made under.

    A run records the ablation cell it belongs to, and replaying it under the
    base configuration would re-run a *different experiment* -- with a
    different visibility policy, a different graph arm, or different grounding
    -- and report the resulting mismatch as non-determinism.
    """
    from cascade.config import load_settings

    overlay = None if config_id in {"base", ""} else config_id
    try:
        return load_settings(overlay)
    except FileNotFoundError:
        return load_settings(None)


def _child_command(run_id: str) -> list[str]:
    return [sys.executable, "-m", "cascade.trace.replay_child", run_id]


def _replay_in_subprocess(target: ReplayTarget, env_file: str | None = None) -> ReplayOutcome:
    """Run one replay in its own interpreter and compare what comes back.

    The child's environment comes from :func:`cascade.config.child_environment`
    rather than from ``os.environ`` here: only ``config.py`` reads the process
    environment (a test enforces it), and a child that re-derived its settings
    location from ambient environment would work from a shell and fail wherever
    that environment is deliberately controlled -- which is exactly where
    determinism is checked.
    """
    from cascade.config import child_environment

    env = child_environment(PYTHONHASHSEED=CHILD_HASH_SEED)
    if env_file is not None:
        env["CASCADE_ENV_FILE"] = env_file
    try:
        completed = subprocess.run(  # noqa: S603 -- fixed argv, no shell
            _child_command(target.run_id),
            capture_output=True,
            text=True,
            timeout=900,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ReplayOutcome(
            run_id=target.run_id,
            expected_hash=target.event_log_hash,
            actual_hash=None,
            error=f"{type(exc).__name__}: {exc}",
            digest_stale=target.digest_stale,
        )

    if completed.returncode == EXIT_CACHE_MISS:
        # The child reached a model call whose recording is not in this
        # machine's cache. The run may well be deterministic; nothing here can
        # say, so it is filed as unreplayable rather than as a divergence.
        try:
            detail = json.loads(completed.stdout.strip().splitlines()[-1]).get("error", "")
        except (ValueError, IndexError, AttributeError):
            detail = (completed.stderr or completed.stdout).strip()[-400:]
        return ReplayOutcome(
            run_id=target.run_id,
            expected_hash=target.event_log_hash,
            actual_hash=None,
            error=str(detail) or "a recording this run needs is not in this machine's cache",
            unreplayable=True,
            digest_stale=target.digest_stale,
        )
    if completed.returncode != 0:
        return ReplayOutcome(
            run_id=target.run_id,
            expected_hash=target.event_log_hash,
            actual_hash=None,
            error=(completed.stderr or completed.stdout).strip()[-400:] or "child exited non-zero",
            digest_stale=target.digest_stale,
        )
    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        return ReplayOutcome(
            run_id=target.run_id,
            expected_hash=target.event_log_hash,
            actual_hash=None,
            error=f"child produced no parseable result: {type(exc).__name__}: {exc}",
            digest_stale=target.digest_stale,
        )

    actual_hash = str(payload["event_log_hash"])
    if actual_hash == target.event_log_hash:
        return ReplayOutcome(
            run_id=target.run_id,
            expected_hash=target.event_log_hash,
            actual_hash=actual_hash,
            digest_stale=target.digest_stale,
        )

    # §8.4: the bisect names the first divergent field. Comparing the per-step
    # state hashes turns "these runs differ" into "they differ from step 11",
    # which is the difference between a diagnosis and a symptom.
    replayed = [str(value) for value in payload.get("state_hashes", [])]
    first: int | None = None
    expected_state = actual_state = None
    for index in range(max(len(target.state_hashes), len(replayed))):
        stored = target.state_hashes[index] if index < len(target.state_hashes) else None
        fresh = replayed[index] if index < len(replayed) else None
        if stored != fresh:
            first, expected_state, actual_state = index, stored, fresh
            break

    return ReplayOutcome(
        run_id=target.run_id,
        expected_hash=target.event_log_hash,
        actual_hash=actual_hash,
        first_divergent_step=first,
        expected_state_hash=expected_state,
        actual_state_hash=actual_state,
        digest_stale=target.digest_stale,
    )


def verify_replays(
    targets: Sequence[ReplayTarget], *, workers: int = 4, env_file: str | None = None
) -> ReplayReport:
    """Replay every target in its own process and compare the hashes.

    Concurrency is over *processes*, which is safe because a replay writes
    nothing: it re-simulates and returns hashes. Results are collected in
    target order so a report reads the same way twice (invariant 7).
    """
    import time

    started = time.perf_counter()
    if not targets:
        return ReplayReport(outcomes=[], elapsed_s=0.0)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        outcomes = list(pool.map(lambda target: _replay_in_subprocess(target, env_file), targets))
    return ReplayReport(outcomes=outcomes, elapsed_s=time.perf_counter() - started)


# ---------------------------------------------------------------------------
# Refreshing the cached digest (M17)
#
# `runs.event_log_hash` is derived from the events; the events are the record.
# When the hash domain moves, the honest repair is to recompute the derived
# column from the untouched rows -- as migration 016 corrected `runs.llm_calls`
# at M8 -- and to keep what it replaced.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RehashItem:
    """One stored run's cached digest beside the digest its events hash to today."""

    run_id: str
    stored_digest: str
    current_digest: str

    @property
    def stale(self) -> bool:
        return self.stored_digest != self.current_digest


def plan_rehash(
    settings: Settings,
    *,
    config_id: str | None = None,
    limit: int | None = None,
    role: Role = "eval",
) -> tuple[RehashItem, ...]:
    """Recompute every stored run's digest from its events. Reads only.

    Preserves the property that the plan is a function of the stored rows and
    the current ``canonical()``: nothing here depends on when a run was made
    or by which decider, so two operators planning the same database see the
    same stale set.
    """
    targets = load_replay_targets(
        settings,
        limit=limit if limit is not None else 1_000_000_000,
        config_id=config_id,
        role=role,
    )
    return tuple(
        RehashItem(
            run_id=target.run_id,
            stored_digest=target.stored_digest or "",
            current_digest=target.event_log_hash,
        )
        for target in targets
    )


def apply_rehash(settings: Settings, items: Sequence[RehashItem], *, archive: Path) -> int:
    """Write the recomputed digests to ``runs.event_log_hash`` for the stale items.

    Preserves invariant 6 by construction: the only table touched is ``runs``,
    through the admin role, and the events it reads are never written. Before
    any row changes, every ``(run_id, from, to)`` triple is written to
    ``archive``, so the digests being replaced stay recoverable. Each UPDATE is
    conditional on the digest it expects to replace, so a row changed by
    someone else between the plan and the apply is left alone and shows up as
    a smaller count than planned. Returns the number of rows updated.
    """
    stale = [item for item in items if item.stale]
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text(
        json.dumps(
            {
                "written_at": datetime.now(UTC).isoformat(),
                "reason": (
                    "runs.event_log_hash recomputed from the stored events under the current "
                    "DecisionEvent.canonical() (M16 removed cache_hit; found at M17)"
                ),
                "rehashed": [
                    {"run_id": item.run_id, "from": item.stored_digest, "to": item.current_digest}
                    for item in sorted(stale, key=lambda item: item.run_id)
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if not stale:
        return 0
    updated = 0
    with _connect(settings, "admin") as conn:
        with conn.cursor() as cur:
            for item in sorted(stale, key=lambda item: item.run_id):
                cur.execute(
                    "UPDATE runs SET event_log_hash = %s WHERE run_id = %s AND event_log_hash = %s",
                    (item.current_digest, item.run_id, item.stored_digest),
                )
                updated += int(cur.rowcount)
        conn.commit()
    return updated
