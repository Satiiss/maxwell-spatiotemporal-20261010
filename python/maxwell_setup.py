"""Official MaxLab hardware adapter. Only changed active units are touched per trial."""
from __future__ import annotations

import importlib
import logging
import subprocess
import sys
import time
from pathlib import Path

from analysis.utils.io import parse_cfg


def require_ok(value, action):
    if str(value).strip().lower() not in ("ok", "0"):
        raise RuntimeError(f"{action} 失败: {value!r}")


class MaxLabBackend:
    realtime = True

    def __init__(self, system, reporter):
        self.system, self.reporter = system, reporter
        self.recording = self.file_open = False
        self.array = self.mx = None
        self.pool = {}
        self.active = {}
        self.monitor = None
        self.all_units = set()
        self.monitor_log = None

    def initialize(self, cfg):
        parent = self.system["maxwell"].get("api_parent")
        if parent:
            sys.path.insert(0, str(Path(parent).resolve()))
        self.mx = importlib.import_module("maxlab")
        mx = self.mx
        self.mapping = parse_cfg(cfg)
        mx.initialize()
        mx.activate([self.system["maxwell"]["well"]])
        require_ok(mx.send(mx.Core().enable_stimulation_power(True)), "刺激电源初始化")
        require_ok(mx.send(mx.Amplifier().set_gain(self.system["maxwell"]["amplifier_gain"])), "设置gain")
        mx.set_event_threshold(self.system["maxwell"]["event_threshold"])
        self.reporter.wait(mx.Timing.waitInit, "设备初始化等待")
        mx.clear_events()  # Exactly before defining any event/sequence.
        self.array = mx.Array("spatiotemporal")
        self.lsb_mv = float(mx.query_DAC_lsb_mV())
        self.cfg = cfg
        self.route([])
        if self.system["maxwell"]["monitor_enabled"]:
            executable = Path(self.system["maxwell"]["monitor_executable"]).resolve()
            log_path = cfg.parent / "cpp_monitor.log"
            self.monitor_log = log_path.open("w", encoding="utf-8")
            self.monitor = subprocess.Popen([str(executable), str(self.system["maxwell"]["well"])],
                                            stdout=self.monitor_log, stderr=subprocess.STDOUT)
            self.reporter.wait(2, "等待被动C++数据流")
            self.check_monitor()

    def route(self, electrodes):
        mx, array = self.mx, self.array
        self.disconnect_active()
        require_ok(array.load_config(str(self.cfg)), "加载CFG")
        # Remove pre-existing stimulation links before selecting this pool; never per pulse.
        for row in self.mapping:
            linked = array.query_stimulation_at_electrode(row["electrode"])
            if str(linked).strip():
                require_ok(array.disconnect_electrode_from_stimulation(row["electrode"]), "移除旧刺激链接")
        pool = {}
        for electrode in electrodes:
            require_ok(array.connect_electrode_to_stimulation(electrode), f"路由电极{electrode}")
            text = str(array.query_stimulation_at_electrode(electrode)).strip()
            if not text.isdigit() or not 0 <= int(text) < self.system["routing"]["maximum_stim_units"]:
                raise RuntimeError(f"电极{electrode}无法分配刺激单元: {text!r}")
            unit = int(text)
            if unit in pool.values():
                raise RuntimeError("多个电极共用同一stim unit，停止实验")
            pool[electrode] = unit
            self.all_units.add(unit)
            require_ok(mx.send(mx.StimulationUnit(unit).power_up(True).connect(False).set_voltage_mode().dac_source(0)), "准备stim unit")
        array.download([self.system["maxwell"]["well"]])
        self.reporter.wait(mx.Timing.waitAfterDownload, "路由下载等待")
        mx.offset()
        wait = mx.Timing.waitInMX2Offset if self.system["maxwell"]["device"] == "maxtwo" else mx.Timing.waitInMX1Offset
        self.reporter.wait(wait, "偏置校准等待")
        actual = {r.electrode: (r.channel, r.x, r.y) for r in array.get_config().mappings}
        expected = {r["electrode"]: (r["channel"], r["x"], r["y"]) for r in self.mapping}
        if actual != expected:
            raise RuntimeError("刺激路由改变了记录电极映射，需要人工核对；不继续刺激")
        self.pool, self.active = pool, {}
        logging.info("路由完成: %s，所有unit初始断开", pool)
        return pool

    def disconnect_active(self):
        if self.mx:
            for electrode in list(self.active):
                require_ok(self.mx.send(self.mx.StimulationUnit(self.pool[electrode]).power_up(True).connect(False)
                                        .set_voltage_mode().dac_source(self.active[electrode])), "断开active unit")
        self.active = {}

    def switch(self, electrodes):
        desired = {int(e): dac for dac, e in enumerate(electrodes)}
        if len(desired) != len(electrodes) or len(desired) > 2 or any(e not in self.pool for e in desired):
            raise ValueError("当前刺激条件必须包含1或2个已路由的不同电极")
        changed = False
        for electrode, dac in self.active.items():
            if desired.get(electrode) != dac:
                require_ok(self.mx.send(self.mx.StimulationUnit(self.pool[electrode]).power_up(True).connect(False)
                                        .set_voltage_mode().dac_source(dac)), "快切断开上一组")
                changed = True
        for electrode, dac in desired.items():
            if self.active.get(electrode) != dac:
                require_ok(self.mx.send(self.mx.StimulationUnit(self.pool[electrode]).power_up(True).connect(True)
                                        .set_voltage_mode().dac_source(dac)), "快切接入当前组")
                changed = True
        self.active = desired
        if changed:
            self.reporter.wait_until(time.monotonic() + self.system["routing"]["settle_wait_ms"] / 1000)

    def start_segment(self, directory, basename):
        self.directory, self.basename = Path(directory), basename
        self.saver = self.mx.Saving()
        self.saver.set_legacy_format(False)
        self.saver.open_directory(str(self.directory))
        self.saver.start_file(basename)
        self.file_open = True
        self.saver.group_delete_all()
        channels = [r["channel"] for r in self.mapping]
        require_ok(self.saver.group_define(self.system["maxwell"]["well"], "exp", channels), "定义记录group")
        self.array.save_config(str(self.directory / "routed.cfg"))
        self.record_epoch = time.time()
        self.saver.start_recording([self.system["maxwell"]["well"]])
        self.recording = True
        return self.record_epoch

    def prepare_sequence(self, condition, commands, first_id, protocol):
        seq = self.mx.Sequence(f"st_{first_id}", initial_delay=protocol["sequence_initial_delay_samples"], persistent=False)
        for opcode, args in commands:
            if opcode == "dac":
                seq.append(self.mx.DAC(*args))
            elif opcode == "delay":
                seq.append(self.mx.DelaySamples(*args))
            elif opcode == "event":
                event_id, pulse_index = args
                props = f"condition {condition.name} pulse {pulse_index} kind {condition.kind} dt_ms {condition.dt_ms:g}"
                seq.append(self.mx.Event(self.system["maxwell"]["well"], 1, event_id, props))
        return seq

    def stimulate(self, seq):
        self.check_monitor()
        epoch = time.time()
        # Sequence.send discards the response in this SDK, so check the official raw command directly.
        require_ok(self.mx.send_raw("sequence_send " + seq.token), "下发刺激Sequence")
        return epoch, None

    def advance(self, seconds):
        self.reporter.wait(seconds, "录制中")

    def check_monitor(self):
        if self.monitor is not None and self.monitor.poll() is not None:
            raise RuntimeError(f"被动C++读取器提前退出，code={self.monitor.returncode}；查看cpp_monitor.log")

    def finish_segment(self):
        self._close_recording()
        candidates = list(self.directory.glob(self.basename + "*.raw.h5"))
        if len(candidates) != 1:
            raise RuntimeError(f"录制关闭后未找到唯一H5: {self.directory}/{self.basename}*.raw.h5")
        return candidates[0]

    def _close_recording(self):
        if self.recording:
            self.saver.stop_recording()
            self.recording = False
            # Cleanup must run even if Stop is pressed; do not use a stop-aware wait here.
            time.sleep(max(self.system["maxwell"]["wait_after_recording_sec"], self.mx.Timing.waitAfterRecording))
        if self.file_open:
            self.saver.stop_file()
            self.file_open = False
            self.saver.group_delete_all()

    def abort_segment(self):
        self._close_recording()

    def shutdown(self):
        errors = []
        # Stop electrical output before waiting for recording flush.
        for action in (self.disconnect_active, self._close_recording):
            try:
                action()
            except Exception as exc:
                errors.append(str(exc))
                logging.exception("硬件收尾失败")
        if self.mx:
            for unit in self.all_units:
                try:
                    require_ok(self.mx.send(self.mx.StimulationUnit(unit).power_up(False).connect(False)), "关闭刺激unit")
                except Exception as exc:
                    errors.append(str(exc))
            for dac in (0, 1, 2):
                try:
                    require_ok(self.mx.send(self.mx.DAC(dac, 512)), "DAC回中性")
                except Exception as exc:
                    errors.append(str(exc))
            try:
                require_ok(self.mx.send(self.mx.Core().enable_stimulation_power(False)), "关闭刺激电源")
            except Exception as exc:
                errors.append(str(exc))
        if self.monitor is not None:
            try:
                if self.monitor.poll() is None:
                    self.monitor.terminate()
                self.monitor.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.monitor.kill()
                self.monitor.wait()
                errors.append("被动读取器未能正常关闭数据流，需要检查mxwserver")
            if self.monitor.returncode != 0:
                errors.append(f"被动读取器退出code={self.monitor.returncode}；需核对流关闭状态")
        if self.monitor_log:
            self.monitor_log.close()
        if errors:
            raise RuntimeError("硬件收尾不完整：" + "; ".join(errors))
