"""Pure stimulus schedules and voltage command plans; no MaxLab side effects."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math


@dataclass(frozen=True)
class Condition:
    name: str
    kind: str
    electrodes: tuple[int, ...]
    labels: tuple[str, ...]
    dt_ms: float = 0
    relation: str = "single"


def formal_conditions(units, dt_ms):
    """Return single, synchronous and temporal stages, preserving both directions."""
    if len(units) != 3 or any(len(u["electrodes"]) != 2 for u in units):
        raise ValueError("正式实验必须是3个unit，每个unit两个有效单电极")
    sites = {(i, j): int(u["electrodes"][j]) for i, u in enumerate(units, 1) for j in range(2)}
    labels = {(i, j): f"u{i}-{'ab'[j]}" for i in range(1, 4) for j in range(2)}
    singles = [Condition(labels[k], "single", (sites[k],), (labels[k],)) for k in sites]
    pairs = [((i, 0), (i, 1), "local") for i in range(1, 4)]
    pairs += [((a, 0), (b, 0), "cross") for a, b in ((1, 2), (1, 3), (2, 3))]
    sync = [Condition(f"{labels[a]}__{labels[b]}", "synchronous", (sites[a], sites[b]),
                      (labels[a], labels[b]), relation=rel) for a, b, rel in pairs]
    stages = [("single", singles), ("synchronous", sync)]
    for dt in dt_ms:
        stage = []
        for a, b, rel in pairs:
            for first, second in ((a, b), (b, a)):
                name = f"{labels[first]}__{labels[second]}__dt{float(dt):g}ms"
                stage.append(Condition(name, "temporal", (sites[first], sites[second]),
                                       (labels[first], labels[second]), float(dt), rel))
        stages.append((f"temporal_dt{float(dt):g}ms", stage))
    return stages


def round_robin(conditions, rounds):
    for round_index in range(1, int(rounds) + 1):
        for condition in conditions:
            yield round_index, condition


def pulse_duration_sec(protocol):
    return (2 * float(protocol["pulse_width_us"]) + float(protocol["inter_phase_interval_us"])) * 1e-6


def next_onset_interval(condition, formal_cfg, pulse_duration_sec):
    interval = float(formal_cfg["trial_interval_sec"])
    if condition.kind == "temporal" and formal_cfg["temporal_interval_mode"] == "after_second":
        # The complete second biphasic waveform must end before the one-second rest starts.
        return condition.dt_ms / 1000 + pulse_duration_sec + interval
    if interval < condition.dt_ms / 1000 + pulse_duration_sec + 0.05:
        raise ValueError("第一脉冲间隔不足：会与第二脉冲或其50ms响应窗重叠；请选after_second或增大间隔")
    return interval


def waveform_commands(condition, protocol, lsb_mv, first_event_id):
    """Return (opcode,args) list. DelaySamples ticks are fixed 50us, not H5 fs."""
    amplitude = float(protocol["amplitude_mv"])
    if not math.isfinite(lsb_mv) or lsb_mv <= 0 or not math.isfinite(amplitude) or amplitude <= 0:
        raise ValueError("DAC LSB/幅值必须是正的有限值")
    bits = int(round(amplitude / lsb_mv))
    if not 1 <= bits <= 511:
        raise ValueError("刺激幅值超出DAC电压模式范围，不允许截断")
    tick = float(protocol["sequence_tick_us"])
    if tick != 50:
        raise ValueError("官方DelaySamples步长固定为50us")
    def samples(us):
        count = int(round(us / tick))
        if not math.isclose(count * tick, us, abs_tol=1e-6):
            raise ValueError(f"时序 {us} us 不能由50us步长精确表示")
        return count
    phase = samples(float(protocol["pulse_width_us"]))
    gap = samples(float(protocol["inter_phase_interval_us"]))
    if phase < 1 or gap < 0 or protocol["polarity"] != "negative_first":
        raise ValueError("本实验要求有效单相脉宽和先负后正双相脉冲")
    negative, positive = 512 + bits, 512 - bits
    commands = []
    def pulse(mode, event_id, pulse_index):
        commands.append(("event", (event_id, pulse_index)))
        commands.append(("dac", (mode, negative, negative)))
        commands.append(("delay", (phase,)))
        if gap:
            commands.extend([("dac", (mode, 512, 512)), ("delay", (gap,))])
        commands.extend([("dac", (mode, positive, positive)), ("delay", (phase,)),
                         ("dac", (mode, 512, 512))])
    if condition.kind == "synchronous":
        pulse(3, first_event_id, 1)  # Simultaneous DAC0 and DAC1 update.
    else:
        pulse(0, first_event_id, 1)
        if condition.kind == "temporal":
            delta = samples(condition.dt_ms * 1000) - 2 * phase - gap
            if delta < 0:
                raise ValueError("dt必须不短于第一双相脉冲总时长")
            if delta:
                commands.append(("delay", (delta,)))
            pulse(1, first_event_id + 1, 2)
    return commands


def condition_dict(condition):
    return asdict(condition)
