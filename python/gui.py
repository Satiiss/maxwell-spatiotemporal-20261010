"""Chinese Tk UI. Worker performs hardware/analysis; main thread only displays queued state."""
from __future__ import annotations

import copy
import logging
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .utils.progress import Reporter


def launch(system, stimulation, args):
    root = tk.Tk()
    root.title("MaxWell 空间与时序刺激实验")
    root.geometry("1080x800")
    events = queue.Queue()
    stop = threading.Event()
    reporter = Reporter(events.put, stop)
    worker = None
    vars = {}
    header = ttk.LabelFrame(root, text="实验配置")
    header.pack(fill="x", padx=12, pady=8)
    initial = {"cfg": system["electrode_map"]["cfg_path"] or "", "baseline": system["manual_inputs"]["baseline_h5"] or "",
               "centers": ",".join(map(str, system["manual_inputs"]["candidate_centers"])),
               "culture": system["culture"]["id"] or "", "div": "" if system["culture"]["div"] is None else str(system["culture"]["div"])}
    for row, (key, label) in enumerate((("cfg", "电极配置 CFG"), ("baseline", "已有基线 H5（可空，自动记录10 min）"),
                                        ("centers", "5–6 个手动候选中心电极 ID"), ("culture", "培养物编号"), ("div", "培养天数 DIV"))):
        ttk.Label(header, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=3)
        vars[key] = tk.StringVar(value=initial[key])
        ttk.Entry(header, textvariable=vars[key], width=74).grid(row=row, column=1, sticky="ew", padx=6)
        if key in ("cfg", "baseline"):
            def browse(k=key):
                value = filedialog.askopenfilename(filetypes=[("CFG" if k == "cfg" else "H5", "*.cfg" if k == "cfg" else "*.h5")])
                if value:
                    vars[k].set(value)
            ttk.Button(header, text="选择文件", command=browse).grid(row=row, column=2)
    mode = tk.StringVar(value="模拟" if args.mode == "mock" else "真实硬件")
    formula = tk.StringVar(value={None: "待明确", "mean_plus_3sd": "均值 + 3倍标准差", "mean_plus_3variance": "均值 + 3倍方差"}[stimulation["probe"]["threshold_rule"]])
    interval = tk.StringVar(value="第二脉冲结束后1秒" if stimulation["formal"]["temporal_interval_mode"] == "after_second" else "第一脉冲间隔1秒")
    quick = tk.BooleanVar(value=args.quick)
    probe_only = tk.BooleanVar(value=True)
    options = ttk.Frame(header)
    options.grid(row=5, column=0, columnspan=3, sticky="w", padx=6, pady=6)
    ttk.Combobox(options, textvariable=mode, values=["模拟", "真实硬件"], state="readonly", width=12).pack(side="left", padx=4)
    ttk.Combobox(options, textvariable=formula, values=["待明确", "均值 + 3倍标准差", "均值 + 3倍方差"], state="readonly", width=22).pack(side="left", padx=4)
    ttk.Combobox(options, textvariable=interval, values=["第二脉冲结束后1秒", "第一脉冲间隔1秒"], state="readonly", width=20).pack(side="left", padx=4)
    ttk.Checkbutton(options, text="缩短模拟数据", variable=quick).pack(side="left", padx=4)
    start_text = tk.StringVar(value="开始Probe" if probe_only.get() else "开始实验")
    ttk.Label(header, text="本窗口仅运行Probe，完成后停止。" ).grid(row=6, column=0, columnspan=3, sticky="w", padx=10)
    ttk.Label(header, text="正式实验须审阅Probe结果后，用终端--formal-from另行启动。" ).grid(
        row=7, column=0, columnspan=3, sticky="w", padx=10, pady=3)
    status = tk.StringVar(value="待启动；真实输入可暂时留空")
    ttk.Label(root, textvariable=status, wraplength=1000).pack(fill="x", padx=12)
    ttk.Label(root, text="总任务进度").pack(anchor="w", padx=12)
    overall = ttk.Progressbar(root, maximum=100)
    overall.pack(fill="x", padx=12)
    ttk.Label(root, text="当前子任务进度").pack(anchor="w", padx=12)
    sub = ttk.Progressbar(root, maximum=100)
    sub.pack(fill="x", padx=12)
    details = tk.StringVar(value="")
    ttk.Label(root, textvariable=details, wraplength=1000).pack(fill="x", padx=12, pady=6)
    map_frame = ttk.LabelFrame(root, text="当前CFG候选位点与刺激电极")
    map_frame.pack(fill="x", padx=12)
    map_canvas = tk.Canvas(map_frame, height=140, background="white", highlightthickness=0)
    map_canvas.pack(fill="x")
    map_positions = {}

    def show_map(mapping, units):
        map_canvas.delete("all")
        map_positions.clear()
        if not mapping:
            return
        xs, ys = [r["x"] for r in mapping], [r["y"] for r in mapping]
        sx, sy = max(max(xs) - min(xs), 17.5), max(max(ys) - min(ys), 17.5)
        width = max(900, map_canvas.winfo_width())
        scale = min((width - 70) / sx, 95 / sy)
        for row in mapping:
            x, y = 35 + (row["x"] - min(xs)) * scale, 15 + (row["y"] - min(ys)) * scale
            map_positions[row["electrode"]] = (x, y)
            map_canvas.create_oval(x-1, y-1, x+1, y+1, fill="#D8DEE9", outline="")
        for i, unit in enumerate(units, 1):
            for electrode in unit["electrodes"]:
                x, y = map_positions[electrode]
                map_canvas.create_oval(x-3, y-3, x+3, y+3, fill="#A4C8E1", outline="#303C43")
            x, y = map_positions[unit["center"]]
            map_canvas.create_text(x, y - 9, text=f"★ {i}", fill="#303C43")
        bar = 100 * scale
        map_canvas.create_line(35, 127, 35 + bar, 127, width=2, fill="#303C43")
        map_canvas.create_text(35 + bar/2, 117, text="100 µm", fill="#303C43")

    def mark_current(electrodes):
        map_canvas.delete("current")
        for electrode in electrodes:
            if electrode in map_positions:
                x, y = map_positions[electrode]
                map_canvas.create_rectangle(x-7, y-7, x+7, y+7, outline="#F26B59", width=2, tags="current")
                map_canvas.create_line(x + 25, y - 20, x+8, y-6, arrow="last", fill="#F26B59", width=2, tags="current")
    columns = ("unit", "ele", "trials", "prob", "artifact", "response", "eligible")
    table = ttk.Treeview(root, columns=columns, show="headings", height=7)
    for key, title in zip(columns, ("候选unit", "电极", "有效/总trial", "响应概率", "0–3ms伪迹均值", "10–50ms响应均值", "可用")):
        table.heading(key, text=title)
        table.column(key, width=130, anchor="center")
    table.pack(fill="x", padx=12, pady=6)
    logs = tk.Text(root, height=12, wrap="word", font=("Microsoft YaHei", 9))
    logs.pack(fill="both", expand=True, padx=12)

    class QueueLog(logging.Handler):
        def emit(self, record):
            events.put({"log": self.format(record)})
    log_handler = QueueLog()
    logging.getLogger().addHandler(log_handler)
    controls = ttk.Frame(root)
    controls.pack(fill="x", padx=12, pady=8)

    def config_from_ui():
        s, p = copy.deepcopy(system), copy.deepcopy(stimulation)
        s["electrode_map"]["cfg_path"] = vars["cfg"].get().strip() or None
        s["manual_inputs"]["baseline_h5"] = vars["baseline"].get().strip() or None
        centers = vars["centers"].get().replace("，", ",").replace(";", ",").replace(" ", ",")
        s["manual_inputs"]["candidate_centers"] = [int(e) for e in centers.split(",") if e.strip()]
        s["culture"]["id"] = vars["culture"].get().strip() or None
        s["culture"]["div"] = int(vars["div"].get()) if vars["div"].get().strip() else None
        p["probe"]["threshold_rule"] = {"待明确": None, "均值 + 3倍标准差": "mean_plus_3sd", "均值 + 3倍方差": "mean_plus_3variance"}[formula.get()]
        p["formal"]["temporal_interval_mode"] = "after_second" if interval.get() == "第二脉冲结束后1秒" else "onset_to_onset"
        return s, p

    def start(dry_run=False):
        nonlocal worker
        if worker and worker.is_alive():
            return
        try:
            s, p = config_from_ui()
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        stop.clear()
        reporter.consume_probe_action()
        start_button.configure(state="disabled")
        table.delete(*table.get_children())
        # Read Tk variables only on the UI thread.
        selected_mode, selected_quick, selected_probe_only = mode.get(), quick.get(), probe_only.get()
        def safe_job():
            try:
                from main import execute
                path = execute(s, p, "mock" if selected_mode == "模拟" else "hardware", reporter=reporter,
                               dry_run=dry_run, quick=selected_quick, compile_cpp=args.build_cpp,
                               probe_only=selected_probe_only)
                events.put({"finished": str(path), "dry_run": dry_run, "probe_only": selected_probe_only})
            except BaseException as exc:
                events.put({"error": str(exc)})
        worker = threading.Thread(target=safe_job, daemon=False)
        worker.start()

    start_button = ttk.Button(controls, textvariable=start_text, command=start)
    start_button.pack(side="left", padx=4)
    ttk.Button(controls, text="只生成调度计划", command=lambda: start(True)).pack(side="left", padx=4)
    ttk.Button(controls, text="停止实验", command=stop.set).pack(side="left", padx=4)
    skip = ttk.Button(controls, text="跳过当前Probe电极", command=lambda: reporter.set_probe_action("skip"), state="disabled")
    skip.pack(side="left", padx=4)
    retry = ttk.Button(controls, text="重新Probe当前电极", command=lambda: reporter.set_probe_action("retry"), state="disabled")
    retry.pack(side="left", padx=4)

    def poll():
        try:
            while True:
                item = events.get_nowait()
                if "log" in item:
                    logs.insert("end", item["log"] + "\n")
                    logs.see("end")
                    # Bound GUI memory; the complete log remains in run/log.txt.
                    if int(logs.index("end-1c").split(".")[0]) > 3000:
                        logs.delete("1.0", "1000.0")
                elif "finished" in item:
                    label = ("计划已保存：" if item.get("dry_run") else
                             "Probe已结束（未开始正式实验）：" if item.get("probe_only") else "执行完成：")
                    status.set(label + item["finished"])
                    start_button.configure(state="normal")
                    overall["value"] = 100
                    skip.configure(state="disabled")
                    retry.configure(state="disabled")
                elif "error" in item:
                    status.set("执行停止/失败：" + item["error"])
                    start_button.configure(state="normal")
                    skip.configure(state="disabled")
                    retry.configure(state="disabled")
                else:
                    phase = item.get("phase", "")
                    if phase == "候选unit已保存":
                        show_map(item["mapping"], item["candidate_units"])
                    if "electrodes" in item:
                        mark_current(item["electrodes"])
                    status.set(phase)
                    sub["value"] = item.get("percent", 0)
                    if item.get("global_total"):
                        overall["value"] = item.get("global_trial", 0) / item["global_total"] * 100
                    info = []
                    for key, label in (("condition", "条件"), ("electrodes", "电极"), ("round", "轮次"),
                                       ("dt_ms", "时序间隔ms"), ("amplitude_mv", "幅值mV"), ("pulse_width_us", "单相脉宽us"),
                                       ("artifact_count", "伪迹"), ("response_count", "响应"), ("h5", "当前H5")):
                        if key in item:
                            info.append(f"{label}：{item[key]}")
                    details.set("；".join(info))
                    enabled = "normal" if phase == "单电极Probe" else "disabled"
                    skip.configure(state=enabled)
                    retry.configure(state=enabled)
                    if phase == "Probe位点评估":
                        table.insert("", "end", values=(item["unit_id"], item["electrode"],
                            f"{item['success_trials']}/{item['all_trials']}", f"{100*item['success_fraction']:.1f}%",
                            f"{item['artifact_0_3ms_mean']:.1f}", f"{item['response_10_50ms_mean']:.1f}", "是" if item["eligible"] else "否"))
        except queue.Empty:
            pass
        root.after(100, poll)

    def close():
        if worker and worker.is_alive():
            stop.set()
            status.set("停止并等待文件关闭、刺激单元收尾后退出")
            def await_close():
                if worker.is_alive():
                    root.after(100, await_close)
                else:
                    root.destroy()
            await_close()
        else:
            root.destroy()
    root.protocol("WM_DELETE_WINDOW", close)
    root.after(100, poll)
    root.mainloop()
    logging.getLogger().removeHandler(log_handler)
    return 0
