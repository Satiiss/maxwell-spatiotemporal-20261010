"""Third-batch independent dt sessions; pure planning and guarded trial execution."""
import copy
from dataclasses import asdict, replace
import math

from .schedule import formal_conditions, round_robin, next_onset_interval, pulse_duration_sec
from .pair_batches import condition_manifest, schedule_manifest, batch_plan

SESSION_DT = {"short": [5, 10], "long": [50, 100]}


def configure_dt_session(stimulation, session="short", *, trials=50,
                         rest_after_sync_sec=300., rest_between_dt_sec=0., final_rest_sec=300.):
    result = copy.deepcopy(stimulation)
    if session not in SESSION_DT:
        raise ValueError("SESSION只能为short或long；一次启动只执行一个时段")
    result["formal"].update(schedule_mode="dt_sessions", session_id=session,
        dt_ms=list(SESSION_DT[session]), trials_per_condition=trials,
        condition_order="cyclic", temporal_interval_mode="after_second",
        rest_after_sync_sec=rest_after_sync_sec, rest_between_dt_sec=rest_between_dt_sec,
        final_rest_sec=final_rest_sec)
    validate_session_config(result["formal"])
    return result


def validate_session_config(cfg):
    if cfg.get("session_id") not in SESSION_DT or cfg.get("dt_ms") != SESSION_DT[cfg["session_id"]]:
        raise ValueError("每时段只能为short=[5,10]或long=[50,100]，不得混入1ms")
    n = cfg.get("trials_per_condition")
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError("每条件trial数必须为正整数；单点参照自动使用同样次数")
    if cfg.get("condition_order") != "cyclic" or cfg.get("temporal_interval_mode") != "after_second":
        raise ValueError("第三批要求cyclic轮转和末脉冲结束后静息")
    for key in ("rest_after_sync_sec", "rest_between_dt_sec", "final_rest_sec",
                "pre_spontaneous_sec", "post_spontaneous_sec", "recording_tail_sec", "trial_interval_sec"):
        x = cfg.get(key)
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0:
            raise ValueError(f"{key}必须是有限非负秒数")
    if cfg["trial_interval_sec"] < .2:
        raise ValueError("第三批至少保留200ms响应窗；默认末脉冲后静息1秒")


def build_session_schedule(units, cfg, *, rounds=None):
    validate_session_config(cfg)
    n = cfg["trials_per_condition"] if rounds is None else rounds
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError("轮数必须为正整数")
    if len({e for u in units for e in u["electrodes"]}) != 6:
        raise ValueError("需要六个不同刺激位点")
    stages = formal_conditions(units, cfg["dt_ms"])
    singles, sync = stages[0][1], stages[1][1]
    steps = []

    def add_stage(name, conditions, checkpoint=""):
        for r, c in round_robin(conditions, n):
            if checkpoint:
                c = replace(c, name=f"{name}__{c.name}", relation="drift_reference")
            pair_id = "__".join(sorted(c.labels)) if c.kind != "single" else ""
            steps.append(dict(action="trial", stage=name, condition=c, round=r,
                batch_id=cfg["session_id"], pair_id=pair_id, checkpoint=checkpoint))

    def rest(name, seconds):
        if seconds > 0:
            steps.append(dict(action="rest", stage=name, seconds=float(seconds),
                batch_id=cfg["session_id"], pair_id="", checkpoint=""))

    add_stage("single_initial", singles)
    add_stage("synchronous", sync)
    rest("rest_after_sync", cfg["rest_after_sync_sec"])
    add_stage("reference_R0", singles, "R0")
    add_stage(stages[2][0], stages[2][1])
    add_stage("reference_R1", singles, "R1")
    if cfg["rest_between_dt_sec"] > 0:
        rest("rest_between_dt", cfg["rest_between_dt_sec"])
        add_stage("reference_after_rest", singles, "after_rest")
    add_stage(stages[3][0], stages[3][1])
    add_stage("reference_R2", singles, "R2")
    rest("rest_final", cfg["final_rest_sec"])
    return steps


def session_plan(steps, system, stimulation):
    result = batch_plan(steps, system, stimulation)
    result.update(schedule_mode="dt_sessions", session_id=stimulation["formal"]["session_id"],
        dt_ms=stimulation["formal"]["dt_ms"], reference_trials=stimulation["formal"]["trials_per_condition"],
        reference_matches_initial_single=True, automatically_starts_next_session=False)
    result["schedule_steps"] = [dict(s, condition=asdict(s["condition"]))
        if s["action"] == "trial" else dict(s) for s in steps]
    return result


def run_dt_session(runner):
    """Use the established sequence lifetime guard, pool-fast routing and H5 logger."""
    from .utils.persistence import atomic_json, write_csv
    cfg = runner.stimulation["formal"]
    rounds = cfg["trials_per_condition"]
    if not runner.backend.realtime and runner.system["mock"].get("trials_per_condition_override"):
        rounds = runner.system["mock"]["trials_per_condition_override"]
    steps = build_session_schedule(runner.formal_units, cfg, rounds=rounds)
    total = sum(s["action"] == "trial" for s in steps)
    runner.global_total = runner.completed_trials + total
    block = "spatiotemporal"
    directory = runner.run_dir / "raw_data" / block
    directory.mkdir(parents=True, exist_ok=True)
    plan = session_plan(steps, runner.system, runner.stimulation)
    # Mock override affects references too; record actual repeat count explicitly.
    plan["reference_trials"] = rounds
    atomic_json(directory / "session_schedule.json", plan)
    write_csv(directory / "condition_manifest.csv", condition_manifest(steps))
    write_csv(directory / "schedule_manifest.csv", schedule_manifest(steps))
    runner.backend.route([e for u in runner.formal_units for e in u["electrodes"]])
    runner.spontaneous(directory / "01_pre_spont", block, "01_pre_spont", cfg["pre_spontaneous_sec"])
    runner.start_segment(directory / "02_stim", block, "02_stim", total)
    deadline = None
    count = 0

    def drain():
        if runner.backend.realtime and deadline is not None:
            runner.reporter.wait_until(deadline)
        runner._formal_sequence = None

    for i, s in enumerate(steps, 1):
        runner.reporter.check()
        if s["action"] == "rest":
            drain()
            runner.backend.disconnect_active() if runner.backend.realtime else runner.backend.switch(())
            runner.audit.add("session_rest", block=block, phase="02_stim", note=s["stage"])
            runner.reporter.wait(s["seconds"], s["stage"], realtime=runner.backend.realtime)
            if not runner.backend.realtime:
                runner.backend.advance(s["seconds"])
            deadline = None
            continue
        runner._trial_context = {k: s[k] for k in ("batch_id", "pair_id", "checkpoint")}
        runner._trial_context.update(session_id=cfg["session_id"], schedule_index=i)
        count += 1
        c = s["condition"]
        period = next_onset_interval(c, cfg, pulse_duration_sec(runner.stimulation["protocol"]))
        deadline = runner.one_trial(c, s["round"], period, count, total, s["stage"], deadline)
    drain()
    runner._trial_context = {}
    runner.backend.disconnect_active() if runner.backend.realtime else runner.backend.switch(())
    runner.reporter.wait(cfg["recording_tail_sec"], "刺激段尾部录制", realtime=runner.backend.realtime)
    if not runner.backend.realtime:
        runner.backend.advance(cfg["recording_tail_sec"])
    runner.finish_segment(total)
    runner.spontaneous(directory / "03_post_spont", block, "03_post_spont", cfg["post_spontaneous_sec"])
    return plan
