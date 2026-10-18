"""Model device execution continuing after sequence_send acknowledges, without an SDK."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

from main import ROOT, load_configuration, make_plan
from python.experiment_runner import ExperimentRunner
from python.schedule import Condition, next_onset_interval, pulse_duration_sec
from python.utils.progress import ExperimentStopped


class Clock:
    def __init__(self):
        self.now, self.pending, self.events = 0., [], []
        self.targets = {}

    def until(self, deadline):
        for when, dac, expected in sorted(self.pending):
            if when <= deadline:
                self.events.append((when, expected, self.targets.get(dac)))
        self.pending = [row for row in self.pending if row[0] > deadline]
        self.now = max(self.now, deadline)


class Sequence:
    def __init__(self, backend, condition, protocol):
        self.backend, self.condition, self.protocol = backend, condition, protocol
        self.retain_until = 0.

    def __del__(self):
        if self.backend.clock.now + 1e-9 < self.retain_until:
            self.backend.deleted_early = True


class Backend:
    realtime, lsb_mv = True, 2.98

    def __init__(self, clock, rest, blocking=False):
        self.clock, self.rest, self.blocking = clock, rest, blocking
        self.switches, self.sends, self.refs = [], [], []
        self.deleted_early = False

    def prepare_sequence(self, condition, commands, event_id, protocol):
        return Sequence(self, condition, protocol)

    def switch(self, electrodes):
        self.switches.append((self.clock.now, tuple(electrodes)))
        self.clock.targets = dict(enumerate(electrodes))
        self.clock.until(self.clock.now + .05)  # Model the actual settling wait.

    def stimulate(self, sequence):
        condition = sequence.condition
        start = self.clock.now + sequence.protocol["sequence_initial_delay_samples"] * 50e-6
        self.clock.pending.append((start, 0, condition.electrodes[0]))
        if condition.kind == "synchronous":
            self.clock.pending.append((start, 1, condition.electrodes[1]))
        if condition.kind == "temporal":
            self.clock.pending.append((start + condition.dt_ms / 1000, 1, condition.electrodes[1]))
        end = start + condition.dt_ms / 1000 + pulse_duration_sec(sequence.protocol)
        sequence.retain_until = end + self.rest
        self.refs.append(weakref.ref(sequence))
        self.sends.append(self.clock.now)
        if self.blocking:
            self.clock.until(end)
        return self.clock.now, None


class FormalTimingChecks(unittest.TestCase):
    def runner(self, rest=1., blocking=False):
        system, stim = load_configuration(ROOT / "config")
        stim["formal"]["trial_interval_sec"] = rest
        clock = Clock()
        backend = Backend(clock, rest, blocking)
        runner = ExperimentRunner.__new__(ExperimentRunner)
        runner.system, runner.stimulation, runner.run_dir = system, stim, ROOT
        runner.backend = backend
        runner.reporter = SimpleNamespace(check=lambda: None, emit=lambda *a, **k: None,
                                           wait_until=clock.until)
        runner.current_log = SimpleNamespace(origin=0., add_trial=lambda row: None,
            block="offline", phase="02_stim", segment_name="async_model")
        runner.audit = SimpleNamespace(add=lambda *a, **k: None)
        runner.event_id = runner.trial_id = 1
        runner.completed_trials = 0
        return runner, backend, clock

    def run_pair(self, kind="temporal", dt=1000., rest=1., blocking=False, pass_deadline=True):
        runner, backend, clock = self.runner(rest, blocking)
        sites = (1,) if kind == "single" else (1, 2)
        first = Condition("first", kind, sites, tuple(map(str, sites)), dt)
        next_sites = (3,) if kind == "single" else (3, 4)
        second = Condition("second", kind, next_sites, tuple(map(str, next_sites)), dt)
        period = next_onset_interval(first, runner.stimulation["formal"],
                                     pulse_duration_sec(runner.stimulation["protocol"]))
        with patch("python.experiment_runner.time.monotonic", side_effect=lambda: clock.now):
            deadline = runner.one_trial(first, 1, period, 1, 2, "offline")
            original = backend.refs[0]()
            self.assertIsNotNone(original)
            required = original.retain_until
            del original
            last = runner.one_trial(second, 1, period, 2, 2, "offline",
                                    deadline if pass_deadline else None)
            clock.until(last)
            runner._formal_sequence = None
        self.assertGreaterEqual(backend.switches[1][0] + 1e-9, required)
        self.assertGreaterEqual(backend.sends[1] - backend.switches[1][0] + 1e-9, .05)
        self.assertFalse(backend.deleted_early)
        self.assertTrue(clock.events)
        self.assertTrue(all(expected == actual for _, expected, actual in clock.events), clock.events)
        return runner, backend, clock

    def test_async_sequences_keep_correct_targets_and_lifetime_through_rest(self):
        for kind, dt in (("single", 0.), ("synchronous", 0.), ("temporal", 1.),
                         ("temporal", 500.), ("temporal", 1000.)):
            for rest in (1., 3.):
                with self.subTest(kind=kind, dt=dt, rest=rest):
                    self.run_pair(kind, dt, rest)

    def test_blocking_send_still_waits_at_least_requested_rest(self):
        self.run_pair(rest=3., blocking=True)

    def test_internal_guard_protects_even_when_caller_omits_deadline(self):
        self.run_pair(pass_deadline=False)

    def test_stop_during_guard_prevents_next_switch_and_send(self):
        runner, backend, clock = self.runner()
        condition = Condition("AB", "temporal", (1, 2), ("A", "B"), 1000.)
        period = next_onset_interval(condition, runner.stimulation["formal"], .0006)
        with patch("python.experiment_runner.time.monotonic", side_effect=lambda: clock.now):
            runner.one_trial(condition, 1, period, 1, 2, "offline")
            def stop(deadline):
                raise ExperimentStopped("stop in quiet interval")
            runner.reporter.wait_until = stop
            with self.assertRaises(ExperimentStopped):
                runner.one_trial(condition, 1, period, 2, 2, "offline")
        self.assertEqual(len(backend.switches), 1)
        self.assertEqual(len(backend.sends), 1)
        clock.until(runner._formal_next_switch_at)
        runner._formal_sequence = None

    def test_invalid_or_response_truncating_rest_rejects(self):
        runner, _, _ = self.runner()
        condition = Condition("AB", "temporal", (1, 2), ("A", "B"), 1000.)
        for value in (False, 0., -.1, .01, float("nan"), float("inf")):
            with self.subTest(rest=value), self.assertRaises(ValueError):
                next_onset_interval(condition, dict(runner.stimulation["formal"], trial_interval_sec=value), .0006)

    def test_plan_accounts_for_rest_initial_delay_and_settling(self):
        system, stim = load_configuration(ROOT / "config")
        original = make_plan(system, stim)
        stim["formal"]["trial_interval_sec"] = 3.
        longer = make_plan(system, stim)
        self.assertEqual(longer["formal_trial_count"], 5400)
        self.assertAlmostEqual(longer["formal_nominal_duration_sec"] - original["formal_nominal_duration_sec"], 10800.)
        self.assertAlmostEqual(original["stages"][0]["nominal_duration_sec"], 300*(1.0006 + .005 + .05))
        self.assertEqual(longer["switch_policy"], "complete_sequence_and_rest_before_switch")


if __name__ == "__main__":
    unittest.main()
