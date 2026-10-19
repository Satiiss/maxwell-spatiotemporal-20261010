# 离线工具

本目录集中存放额外的离线分析、记录检查、Probe复核和绘图入口，以及它们的专用辅助模块。命令在实验根目录执行，不连接MaxWell、不重新刺激。结果仍另存到实验的 `data/`，保留原H5、CFG和配置。

日常Probe使用根目录`run_probe.py`，只改中心列表即可自动匹配三个物理最近邻；
不需要以下两个旧准备工具，见根目录[PROBE.md](../PROBE.md)。它们仅为兼容旧操作记录保留。

| 文件 | 用途 |
|---|---|
| `analyze_baseline.py` | 自发基线burst、网络与候选分析 |
| `prepare_additional_probe.py` | 从原Probe快照及--centers编号或--review草稿生成独立补测配置；自动匹配邻居及新编号，校验来源与旧位点排斥，不刺激 |
| `prepare_repeat_probe.py` | 整轮重测准备；在文件顶部填写来源，恢复来源链全部原unit/四电极/编号，生成新配置，不刺激 |
| `inspect_recording_settings.py` | 只读检查基线及Probe采集设置 |
| `inspect_probe_results.py` | 只读检查Probe逐次响应与录制尾部 |
| `review_probe_rate_variance.py` | 独立发放率方差对照 |
| `plot_probe_rasters.py` | 从现有H5绘制毫秒级局部及刺激对齐图 |
| `review_probe_paired_ratio.py` | 对旧Probe试算后10–50ms严格大于同次前−50至−10ms的1.5倍 |
| `probe_rate_variance.py` | 发放率对照的辅助模块，由入口调用 |
| `probe_raster_review.py` | 刺激对齐和局部绘图辅助模块，由入口调用 |

## 在文件顶部检查新的自发录制

编辑现有`tools/analyze_baseline.py`顶部的`H5_PATH`、`CFG_PATH`及`NETWORK_PARAMETERS`，
保存后固定执行`python -B tools/analyze_baseline.py`。相对路径以项目根目录为基准。
`WELL=0`对应界面的Well 1；未填写H5/CFG时仍可沿用下面的命令行输入。

| 参数 | 原默认值 | 含义 |
|---|---|---|
| `pair_bin_ms` | 1.0 | 电极对延时直方图bin，ms；改变它会改变峰统计，不能简单视为放宽/收紧 |
| `pair_peak_ratio_threshold` | 20.0 | 延时峰比门槛，提高通常更严格 |
| `pair_min_shared_bursts` | 30 | 两电极共享burst数须严格大于此值 |
| `pair_min_abs_delay_ms` | 5.0 | 延时峰绝对值须严格大于此值，单位ms |

顶部参数设为`None`时沿用`config/baseline_analysis.yaml`的值；填数字则覆盖对应项。
原burst和stable-electrode定义保留在YAML。先用原参数分析新600秒Network，随后再逐项调整连接参数。
新录制的`PREVIOUS_ANALYSIS`留空。对同一个H5/CFG做参数复核时，填写上一份输出的`pre_experiment`目录，
继承修改前后参数和revision；换H5或CFG后必须清空，程序拒绝串用旧数据历史。
分析只读输入，不连接设备、不触发Probe，每次结果另存到`data/<新时间>_data/pre_experiment/`。
`parameters.yaml`保存实际生效参数，`network/network_parameter_history.json`记录历史。

图在该输出目录的`network/`下：`baseline_raster.png`看活动时序和记录缺口，
`burst_overview.png`看burst检测范围，`spontaneous_network_all_edges.png`看完整连接，
`spontaneous_network_selected_sites.png`核对中心和三个物理最近邻。
新候选人工确认后，再手动填写Probe中心及本轮采集配置；网络调参不会改变Probe的配对6/10判据。

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

`scripts/prepare_probe_routing.py`涉及SDK服务器软件路由，继续放在`scripts/`；Probe通过根目录`run_probe.py`启动，正式实验通过`run_formal.py`启动，内部复用`main.py`的运行与保存逻辑。

`python -B tools/check_formal_recording.py --enable-hardware`读取现有`run_formal.py`的CONFIG_DIR，
在当前采集状态下另录三秒spikes/events，并核对实际H5边界、CFG映射及无raw；结果另存
`data/<时间>_recording_check/`，不修改源配置或旧结果，不自动进入正式实验。
它不初始化/下载/校准，不改阈值或gain，不发送DAC或刺激单元命令；不应与其他采集同时运行。
省略硬件标志只显示配置，不连接设备。实际采集设置会打印并写入`recording_check.json`供核对。
