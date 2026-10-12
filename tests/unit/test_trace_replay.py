"""The replay verifier's pure half (spec §8.4; M8).

The verdict logic is what decides whether the study's determinism claim passes,
so it is tested without a database or a subprocess. The parts that need both
are in the integration suite.
"""

from __future__ import annotations

from cascade.trace.replay import CHILD_HASH_SEED, ReplayOutcome, ReplayReport, load_settings_for


def outcome(
    run: str, *, actual: str | None, expected: str = "abc", **extra: object
) -> ReplayOutcome:
    return ReplayOutcome(run_id=run, expected_hash=expected, actual_hash=actual, **extra)  # type: ignore[arg-type]


class TestVerdict:
    def test_a_matching_hash_matches(self) -> None:
        assert outcome("r1", actual="abc").matched

    def test_a_differing_hash_does_not(self) -> None:
        assert not outcome("r1", actual="def").matched

    def test_a_child_that_failed_is_not_a_match(self) -> None:
        """An error is not a pass. A replay that could not run has not shown
        that the run is deterministic -- it has shown nothing."""
        assert not outcome("r1", actual=None, error="connection refused").matched

    def test_a_matching_hash_with_an_error_is_still_a_failure(self) -> None:
        """Defence against a child that prints a plausible hash and then dies."""
        assert not outcome("r1", actual="abc", error="died after printing").matched


class TestReport:
    def test_an_all_matching_report_is_ok(self) -> None:
        report = ReplayReport(outcomes=[outcome("a", actual="abc"), outcome("b", actual="abc")])
        assert report.ok
        assert report.matched == 2
        assert report.diverged == ()

    def test_one_divergence_fails_the_whole_report(self) -> None:
        """25 of 25, not 24 of 25: §8.4's rule is that the fix is the source,
        never a tolerance."""
        report = ReplayReport(outcomes=[outcome("a", actual="abc"), outcome("b", actual="zzz")])
        assert not report.ok
        assert report.matched == 1
        assert [item.run_id for item in report.diverged] == ["b"]

    def test_an_empty_report_is_not_ok(self) -> None:
        """Replaying nothing proves nothing, and must not read as a pass."""
        assert not ReplayReport(outcomes=[]).ok

    def test_divergences_preserve_target_order(self) -> None:
        report = ReplayReport(outcomes=[outcome(name, actual="zzz") for name in ("c", "a", "b")])
        assert [item.run_id for item in report.diverged] == ["c", "a", "b"]


class TestBisect:
    def test_a_divergence_carries_the_first_differing_step(self) -> None:
        """§8.4: the bisect names the first divergent field. "The hashes
        differ" is a symptom; "they differ from step 11" is a diagnosis."""
        item = outcome(
            "r1",
            actual="zzz",
            first_divergent_step=11,
            expected_state_hash="aaa",
            actual_state_hash="bbb",
        )
        assert not item.matched
        assert item.first_divergent_step == 11
        assert item.expected_state_hash != item.actual_state_hash


class TestChildIsolation:
    def test_the_child_hash_seed_is_fixed(self) -> None:
        """Fixed so the *child* is reproducible -- a divergence is then a
        property of the code rather than of the run that happened to find it.
        Any fixed value differs from the parent's random one, which is the
        point."""
        assert CHILD_HASH_SEED.isdigit()

    def test_a_run_is_replayed_under_the_configuration_it_was_made_with(self) -> None:
        """Replaying an ablation cell under the base configuration would re-run
        a different experiment -- different visibility policy, different graph
        arm, different grounding -- and report the mismatch as
        non-determinism."""
        assert load_settings_for("C09").flags.causal_decomposition is False
        assert load_settings_for("C01").flags.causal_decomposition is True

    def test_an_unknown_config_falls_back_rather_than_crashing(self) -> None:
        """A run may carry a config id whose overlay has since been renamed.
        Falling back is wrong in a way the hash comparison will catch loudly;
        crashing hides every other run in the batch."""
        assert load_settings_for("no-such-overlay") is not None

    def test_the_base_configuration_needs_no_overlay(self) -> None:
        assert load_settings_for("base").flags.causal_decomposition is True


class TestKinds:
    """Three kinds of "not a pass", kept apart because their remedies differ (M17)."""

    def test_a_missing_recording_is_unverified_not_a_divergence(self) -> None:
        missing = outcome(
            "r1", actual=None, error="no recorded response for key abc", unreplayable=True
        )
        assert not missing.matched
        assert missing.kind == "unreplayable"
        report = ReplayReport(outcomes=[missing])
        assert not report.ok  # nothing was verified, so nothing passed
        assert report.unverified == (missing,)
        assert report.nondeterministic == ()

    def test_a_differing_hash_is_the_only_determinism_defect(self) -> None:
        differing = outcome("r1", actual="zzz")
        assert differing.kind == "diverged"
        report = ReplayReport(outcomes=[differing])
        assert report.nondeterministic == (differing,)
        assert report.unverified == ()

    def test_a_harness_failure_is_unverified(self) -> None:
        dead = outcome("r1", actual=None, error="connection refused")
        assert dead.kind == "failed"
        assert ReplayReport(outcomes=[dead]).unverified == (dead,)

    def test_a_stale_cached_digest_is_counted_and_never_fails_a_reproduced_run(self) -> None:
        """The events reproduce; only the derived column lagged the hash domain."""
        fine = outcome("r1", actual="abc", digest_stale=True)
        assert fine.matched and fine.kind == "reproduced"
        report = ReplayReport(outcomes=[fine])
        assert report.ok
        assert report.stale_digests == 1


class TestRehash:
    def test_an_item_is_stale_only_when_the_digests_differ(self) -> None:
        from cascade.trace.replay import RehashItem

        assert RehashItem(run_id="r", stored_digest="a", current_digest="b").stale
        assert not RehashItem(run_id="r", stored_digest="a", current_digest="a").stale

    def test_a_target_knows_whether_its_cached_digest_is_stale(self) -> None:
        from cascade.trace.replay import ReplayTarget

        def target(stored: str | None) -> ReplayTarget:
            return ReplayTarget(
                run_id="r",
                scenario_id="s",
                config_id="C09",
                replicate=0,
                policy="heuristic",
                event_log_hash="current",
                state_hashes=("h",),
                outcome_score=0.5,
                decisions=1,
                stored_digest=stored,
            )

        assert target("older").digest_stale
        assert not target("current").digest_stale
        assert not target(None).digest_stale  # built by hand: nothing to compare

    def test_the_child_exit_code_for_a_missing_recording_is_the_cache_miss_code(self) -> None:
        """The parent keys on it, so it must be the project's own code (4)."""
        from pathlib import Path

        from cascade.version import EXIT_CACHE_MISS

        source = Path("cascade/trace/replay_child.py").read_text(encoding="utf-8")
        assert EXIT_CACHE_MISS == 4
        assert "except CacheMiss" in source and "return EXIT_CACHE_MISS" in source
