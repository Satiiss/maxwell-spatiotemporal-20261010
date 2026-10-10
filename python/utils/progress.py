from __future__ import annotations

import logging
import threading
import time


class ExperimentStopped(RuntimeError):
    pass


class Reporter:
    def __init__(self, callback=None, stop_event=None):
        self.callback = callback
        self.stop_event = stop_event or threading.Event()
        self.last_status = {}
        self._probe_action = None
        self._action_lock = threading.Lock()

    def set_probe_action(self, action):
        if action not in ("skip", "retry"):
            raise ValueError("仅支持skip或retry")
        with self._action_lock:
            self._probe_action = action

    def consume_probe_action(self):
        with self._action_lock:
            action = self._probe_action
            self._probe_action = None
            return action

    def check(self):
        if self.stop_event.is_set():
            raise ExperimentStopped("用户停止实验")

    def emit(self, phase, current=0, total=1, **details):
        self.check()
        fraction = min(1.0, max(0.0, current / max(total, 1)))
        status = dict(phase=phase, current=current, total=total, percent=100 * fraction, **details)
        self.last_status = status
        width = 20
        bar = "#" * int(width * fraction) + "-" * (width - int(width * fraction))
        logging.info("当前子任务 %s [%s] %s/%s | %s", phase, bar, current, total, details)
        if self.callback:
            self.callback(status)

    def wait(self, seconds, phase, realtime=True, **details):
        if not realtime:
            self.emit(phase, 0, max(seconds, 1), **details)
            return
        start = time.monotonic()
        while True:
            self.check()
            elapsed = time.monotonic() - start
            if elapsed >= seconds:
                break
            self.emit(phase, elapsed, max(seconds, 1), **details)
            self.stop_event.wait(min(1, max(0, seconds - elapsed)))

    def wait_until(self, deadline):
        while time.monotonic() < deadline:
            self.check()
            self.stop_event.wait(min(0.05, max(0, deadline - time.monotonic())))
