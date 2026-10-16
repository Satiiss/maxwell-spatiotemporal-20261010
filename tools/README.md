# 离线工具

本目录集中存放额外的离线分析、记录检查、Probe复核和绘图入口，以及它们的专用辅助模块。命令在实验根目录执行，不连接MaxWell、不重新刺激。结果仍另存到实验的 `data/`，保留原H5、CFG和配置。

| 文件 | 用途 |
|---|---|
| `analyze_baseline.py` | 自发基线burst、网络与候选分析 |
| `prepare_additional_probe.py` | 从原Probe快照和候选草稿生成独立补测配置；校验来源与旧位点排斥，不刺激 |
| `inspect_recording_settings.py` | 只读检查基线及Probe采集设置 |
| `inspect_probe_results.py` | 只读检查Probe逐次响应与录制尾部 |
| `review_probe_rate_variance.py` | 独立发放率方差对照 |
| `plot_probe_rasters.py` | 从现有H5绘制毫秒级局部及刺激对齐图 |
| `review_probe_paired_ratio.py` | 对旧Probe试算后10–50ms严格大于同次前−50至−10ms的1.5倍 |
| `probe_rate_variance.py` | 发放率对照的辅助模块，由入口调用 |
| `probe_raster_review.py` | 刺激对齐和局部绘图辅助模块，由入口调用 |

示例：

```bash
python -B tools/analyze_baseline.py --h5 /实际路径/data.raw.h5 --cfg /实际路径/config.cfg
python -B tools/inspect_recording_settings.py --run data/20261011_152853_data
python -B tools/inspect_probe_results.py --run data/20261011_152853_data
python -B tools/review_probe_rate_variance.py --run data/20261011_152853_data
python -B tools/review_probe_paired_ratio.py --run /实际路径/源Probe目录
python -B tools/plot_probe_rasters.py --run data/20261011_152853_data --electrodes 23209
```

配对复核使用同次前`[-50,-10)`ms与后`[10,50)`ms全部记录电极计数，严格后计数>1.5倍前计数。
缺失前窗时标记incomplete_pre_window，结果另存，不能作为正式实验来源。新采集的启用与Git更新见根目录PAIRED_RATIO_INSTALL.md。

绘图默认显示−10至50ms；`--window-ms -50 200`可扩展查看范围，去掉`--electrodes 23209`处理所有已完成位点。毫秒级图保存到新建的 `data/<时间>_probe_raster_review/<unit>/ele_<ID>/`，旧的整段`raster`不覆盖。

纵轴为全部记录电极的图：`aligned_electrodes.pdf`（全部trial叠加）、`electrode_artifact_zoom.pdf`（−5至10ms）和`trial_01_electrodes.pdf`等逐次图。每行一个实际记录电极，按真实electrode ID排序，静默电极保留；刻度只显示部分ID以保持可读，完整对应表为`electrode_rows.csv`。`aligned_trials.pdf`的每行则仍是一次刺激。

`scripts/prepare_probe_routing.py`涉及SDK服务器软件路由，继续放在`scripts/`；实验仍通过根目录的`main.py`启动。目录迁移只更新文件位置、引用和命令，不改变分析公式、刺激配置或数据保存位置。
