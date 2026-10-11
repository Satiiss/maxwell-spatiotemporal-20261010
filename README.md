# MaxWell 空间组合与时序刺激实验

本项目按 `实验方案20261009.docx`（正文标题为“实验方案20261010”）和 docs 的 Markdown 规范生成。实现自发基线 burst/出入度 Map、人工复核候选中心、Probe、有效电极筛选、单电极刺激、双电极同步刺激和双电极时序刺激的录制与调度。完整正式响应分析 pipeline 和 ESRC 验证暂不实现。

## 实验目标与假设

考察空间组合及刺激先后顺序是否扩展网络的有效诱发响应状态。正式条件共108个：6个单电极、6个同步组合、12个有向组合×8个dt；每条件50次，共5400 trial。

放置细胞、静置2–3h、Scan、电极分配及自发网络候选中心的人工复核属于方案第一条分隔线以上的现场工作。程序不宣称这些步骤已完成，不自动替换候选中心或降低网络阈值。可导入已有600s自发基线；未提供H5时程序先记录600s自发数据。候选中心仍须由人工提供5–6个电极ID。

## 项目目录

```text
spatiotemporal_20261010/
  main.py
  analyze_baseline.py           # 独立离线基线分析，不接触硬件
  setup.py
  requirements.txt
  config/system.yaml
  config/stimulation.yaml
  config/baseline_analysis.yaml
  python/
    experiment_runner.py
    maxwell_setup.py
    mock_backend.py
    schedule.py
    site_selection.py
    figures.py
    gui.py
    utils/{persistence,progress,time_log}.py
  analysis/
    analysis_utils.md
    utils/io.py
    utils/probe.py
    utils/spontaneous.py
    utils/network_plots.py
    utils/detection/burst.py
  cpp/CMakeLists.txt
  cpp/src/spike_monitor.cpp
  scripts/build_cpp.sh
  scripts/inspect_recording_settings.py # 只读核对基线/Probe元数据
  tests/test_experiment.py
  data/{YYYYMMDD_HHMMSS}_data/
```

`python`负责配置、录制、路由及服务器Sequence；本实验为开环调度，没有依赖实时spike触发的闭环算法。C++仅提供可选的被动IIR数据流监视，不发刺激。它在使用前自动编译，通过Python子进程管理，错误退出会阻止后续刺激。

## 参数

| 项目 | 参数或规则 |
|---|---|
| 培养物 | `culture.id`、`culture.div`留空，现场填写 |
| CFG | `electrode_map.cfg_path: null`，现场填写 |
| 候选中心 | `manual_inputs.candidate_centers: []`，填写5–6个电极ID |
| 中心距离 | 微米坐标欧氏距离严格>3×17.5µm |
| unit | 中心+当前CFG中物理最近的3个电极；重复、重叠和不可路由情况报错 |
| Probe | 每unit的4个单电极各10次、2Hz、400mV、单相300µs、先负后正；每次只连接当前电极 |
| Probe响应 | [10,50)ms population spike count，与去burst的同宽40ms背景窗口分布比较 |
| Probe有效性 | 响应成功比例严格>0.60；10次至少7次。0–3ms伪迹仅作刺激送达排查指标 |
| 正式选点 | 合格unit的有效电极数量优先，其次响应概率；选3unit×2电极 |
| 正式刺激 | 每条件50trial；条件循环交叉，不连续重复同条件50次 |
| 同步组合 | 3个unit内组合+3个最优a电极跨unit组合 |
| dt | 1、5、10、20、50、100、500、1000ms，均为第一与第二脉冲起始边沿差 |
| 休息 | 单电极、同步及每个dt阶段后各300s；期间断开active，继续录制 |
| 前后自发 | 正式刺激前600s，最后休息后再600s |
| 刺激路由 | Probe用single_connect，每个位点整段10次后再换路由；正式实验用pool_fast，每trial仅修改上一组/当前组 |
| 两个刺激单元 | 必须不同；DAC0/1分担，同步用 `DAC(3, code0, code1)` |
| 脉冲 | 查询真实LSB；512+bits负相→512−bits正相→512中性 |
| 图示 | 英文文字、白底无网格、600dpi自动PNG+PDF+SVG；空间图100µm比例尺 |

方案中 `[u3-1,u3-b]` 按图示和前文命名理解为 `[u3-a,u3-b]`。300µs按Markdown的单相脉宽定义实现，双相总时长600µs。400mV为每相相对于0V的绝对幅值。序列DelaySamples的50µs步长来自本地官方 `system.py`，独立于记录采样率。

## Probe阈值定义与已确认的时序间隔

`probe.threshold_rule`暂为null。可选：

- `mean_plus_3variance`：响应计数 > 同宽非burst背景均值 + 3×方差，保留方案字面公式。
- `mean_plus_3sd`：响应计数 > 同宽非burst背景均值 + 3×标准差。

真实执行前必须明确选择，程序不隐式将方差替换成标准差。模拟模式临时使用mean_plus_3sd，并在本次参数快照中标明；原始配置仍保持null。

用户已确认 `formal.temporal_interval_mode: after_second`：一对时序刺激的第二个双相脉冲完整结束后，等待1s，再开始下一对。因此第一脉冲起始间隔为dt+第二双相脉冲总时长+1s；单相300µs、相间隔0时，总时长为600µs。每个固定dt循环交叉12个有向电极对，共50轮、600trial，全部完成后休息5min，再进入下一个dt。`onset_to_onset`仅保留为其他协议的可配置选项，不用于本次确认的方案。

## 运行

Python 3.10以上，真实执行在Linux MaxLab设备电脑上进行；Windows可运行模拟、离线分析、配置和计划。现场已有 Conda `maxlab` 环境且 numpy/h5py/yaml/matplotlib 已齐全，可直接使用该环境，无须重新创建或安装依赖。

仓库现为公开项目。Linux 上可直接克隆代码；已有非空项目目录应先完成原地仓库初始化，再拉取更新：

```bash
git clone https://github.com/Satiiss/maxwell-spatiotemporal-20261010.git /home/maxwell/Desktop/maxwell_platform/experiments/spatiotemporal_20261010
conda activate maxlab
cd /home/maxwell/Desktop/maxwell_platform/experiments/spatiotemporal_20261010
```

```bash
python main.py --dry-run
python main.py --mode mock --headless --quick
python main.py --mode mock --gui
```

`--dry-run`只保存完整条件数量、时间/存储估算和参数快照，不加载SDK、不联网、不生成伪造的真实数据。`--quick`仅缩短模拟正式trial数为每条件2次、模拟初始基线为40s；Probe仍为每电极10次。真实模式忽略模拟覆盖参数。

真实运行：先安装官方maxlab包，填写配置；CFG、基线、候选中心、培养物编号、DIV及Probe公式均在现场确定。GUI的文件与公式字段只影响本次运行的快照，不覆盖原始YAML。

```bash
python main.py --mode hardware --enable-hardware --headless
python main.py --mode hardware --gui
```

先单独进行Probe时，使用：

```bash
python -B main.py --mode hardware --gui --probe-only
```

此命令只打开配置窗口；填写现场输入并点击“开始Probe”后才执行硬件操作。GUI固定仅执行Probe：按原方案完成基线准备、全部候选位点Probe和有效位点筛选，然后关闭文件、断开刺激连接并停止；不生成正式实验的刺激或前/后自发录制。合格时状态为`PROBE_COMPLETE`，`experiment_result.json`中的`formal_trial_count`为0。命令行未指定阶段也默认只执行Probe，始终等待人工审阅。

无GUI时，在填写配置后使用`--mode hardware --enable-hardware --headless --probe-only`。`--dry-run --probe-only`只生成Probe计划；`--mode mock --headless --quick --probe-only`可模拟检查。0–3ms伪迹用于诊断，不参与有效刺激判定；本实验入口会将旧配置`enforce_artifact_qc: true`覆盖为false并在日志和快照中记录，原始YAML不修改。响应统计全部记录电极，使用去burst的同宽40ms背景窗口；`mean_plus_3variance`明确指窗口计数均值+3倍计数方差，另保存背景Hz。Probe结束自动保存各unit完整有效位点列表`probe_unit_summary.json/csv`及建议的3×2选点。若不足3个各有至少2个有效单电极的unit，保存`PROBE_REVIEW_REQUIRED`，等待人工排查API/0–3ms伪迹及培养物活性后再启动Probe，不自动反复刺激。

审阅Probe汇总及`pre_experiment/selected_formal_sites.yaml`后，正式实验独立启动：

```bash
python -B main.py --mode hardware --enable-hardware --headless --formal-from /absolute/path/to/completed_probe_run
```

传入Probe的整个run目录。正式入口在接触硬件前验证完成状态、真实/模拟标记、培养物、设备/well、CFG哈希、波形/Probe定义，以及选中的3unit×2电极确有完整10次且至少7次成功；可人工调整建议选点，但仅限原unit内有效位点。然后在新run中执行正式前自发、正式刺激和正式后自发，不重新做Probe或初始基线背景分析。保存`probe_source_reference.json`关联源Probe及审阅选点哈希。未提供源Probe时不能启动正式刺激。真实正式实验禁止使用模拟Probe结果。

Probe逐位点使用`single_connect`：保留中心及原来的三个最近记录邻居，每次仅将当前一个电极连接到刺激单元，完成其10次刺激、关闭记录并断开后，再路由下一个电极；同一段内不重路由。避免把四个邻近位点同时接入时发生共享刺激单元冲突。每个attempt的`routing.json`及`probe_metrics.json`、汇总CSV保存实际刺激单元编号；不同单电极段可以先后使用同一编号。记录映射仍必须与原CFG完全一致。Probe响应合格不等于正式多电极池可用，正式实验的六个位点仍须通过独立刺激单元检查；冲突时报告具体电极及unit编号并停止，不自动替换邻居或取消检查。

可选C++监视配置：

```yaml
maxwell:
  api_parent: /path/containing/maxlab
  maxlab_root: /path/to/maxlab_lib
  monitor_enabled: true
```

`maxlab_root`同时兼容含maxlab/include的maxlab_lib父目录和直接含include/lib的maxlab目录。只有启用监视器或传入 `--build-cpp` 才依赖CMake和GNU C++20。Conda环境使用其lib目录及运行时搜索路径。

GUI提供两层进度、CFG空间图和当前电极箭头、当前条件/电极/轮次/实际协议、位点结果表、停止、Probe跳过和重试。重试保留旧attempt文件，新建attempt目录，不覆盖。主线程只刷新状态，耗时录制、分析和绘图在worker执行。关闭窗口先等待停止和文件收尾完成。

当前Probe指标在每个电极的H5关闭后根据硬件事件帧计算，UI在录制期间明确显示“待文件关闭后计算”，不将主机时间计数当成硬件对齐结果。完整自发连接 Map 可由离线分析输出；逐trial实时伪迹与响应展示、Map嵌入GUI及正式响应分析pipeline属于后续扩展。

## 已有600s基线的离线出入度 Map

H5/CFG映射核对通过后，先分析自发网络，再人工复核5–6个候选中心，随后填写实验配置进行Probe。以下命令只读输入文件，结果只写到本项目新建的 `data/{YYYYMMDD_HHMMSS}_data/pre_experiment/`，不会修改原始H5、CFG、其他实验或Conda环境，也不会连接MaxLab或开始刺激。

```bash
conda activate maxlab
cd /home/maxwell/Desktop/maxwell_platform/experiments/spatiotemporal_20261010
git pull --ff-only
python -B analyze_baseline.py \
  --h5 /home/maxwell/Data/20261011-hzl-28878/261011/28878/Network/000004/data.raw.h5 \
  --cfg /home/maxwell/configs/261011/01h19m58s.cfg
```

默认参数来自 `config/baseline_analysis.yaml`。burst复用现有检测器：1ms bin、50ms平滑、z=6、最短30ms、合并间隔30ms、至少5 spikes；其原有密度回退逻辑保留。需同时查看 `burst_overview` 和 raster，确认检测适合当前培养物。若使用人工复核的burst表，传入 `--bursts-csv /path/to/reviewed.csv`，列为 `start_sec,end_sec`，相对本H5首帧，不得越界或重叠。

连接按每个burst内电极的第一次激活计算：B−A峰延时为正时画A→B，负时画B→A。共同参与burst数严格>30，延时直方图bin=1ms、峰比>=20，峰延时绝对值严格>5ms。单电极首次激活峰比>=10只是稳定标签，不额外筛除电极对。峰比沿用现有定义 `max(bin counts) / median(nonzero bin counts)`；中位数包含峰bin，因此全部样本落在单个bin时峰比为1。程序不会隐式更换统计定义。

全部合格边参与出入度和完整箭头图，未设80条或其他显示上限。静默电极保留在指标表；发放率分母是H5完整录制时长。候选按出度、入度、发放率、平均绝对spike幅值降序，最后按电极ID升序。中心间距严格>52.5µm、3×3物理邻域互斥；每个unit取当前记录映射中物理最近的3个邻居，并排除unit共享电极。保留至多10个候选，前6个画星号。刺激路由可用性仍由后续Probe阶段核对。

输出结构如下（只引用原始H5，不复制3.6GB文件）：

```text
data/{YYYYMMDD_HHMMSS}_data/
  HHhMMmSSs.cfg                 # 原CFG的副本
  log.txt
  pre_experiment/
    source_inputs.json          # H5路径/大小/mtime，CFG的SHA256
    parameters.yaml
    workflow_state.json
    baseline_bursts.csv
    baseline_statistics.json
    candidate_centers_proposed.yaml # 待人工复核草稿，不覆盖system.yaml
    summary.txt
    network/
      stable_electrode_metrics.csv # 全部记录电极及stable_activation标签
      stable_delay_edges.csv
      candidate_sites.csv
      first_activation.npz
      network_parameter_history.{csv,json}
      figure_manifest.json
      baseline_raster.{png,pdf,svg}
      burst_overview.{png,pdf,svg}
      out_degree_map.{png,pdf,svg}
      in_degree_map.{png,pdf,svg}
      spontaneous_network_all_edges.{png,pdf,svg}
      spontaneous_network_selected_sites.{png,pdf,svg}
```

若最大出度<10，保存 `REVIEW_NETWORK_PARAMETERS`，等待人工复核；不足5个中心也不会补入无连接或重叠位点。最大出度达标且有足够中心时仍保存 `REVIEW_CANDIDATES`，不自动开始Probe。

人工参数复核可复制分析YAML到本项目后只修改四个pair参数，然后用相同H5/CFG及 `--config <修改后的YAML> --previous-run <旧pre_experiment目录>` 重算。该模式锁定burst、stable-electrode和其他参数，保留旧结果，并在新结果继承和追加revision历史，两张Map标注revision。人工选择保留低出度参数时，在同一命令添加 `--keep-low-outdegree --review-reason '具体原因'`；只记录人工选择，仍须复核候选及填写Probe配置。

分析绘图文字为英文；终端两层进度与日志为中文。raster超过100万spikes时只抽取显示点，计算仍使用全部spikes，并在图及manifest注明实际显示数量。

## 录制与时间记录

为同时保留方案的一次正式前/后基线和Markdown固定三段结构，正式实验作为一个block：

```text
run_dir/
  HHhMMmSSs.cfg
  log.txt
  external_time_table.csv
  source_inputs.json
  experiment_plan.json
  config_snapshot/{system,stimulation}.yaml
  pre_experiment/
    workflow_state.json
    parameters.yaml
    01_spontaneous/             # 未导入已有基线时
    baseline_reference.json    # 导入已有基线时
    baseline_bursts.csv
    baseline_background_windows.csv
    baseline_statistics.json
    candidate_sites.csv
    candidate_units.{png,pdf,svg}
    02_probe/candidate_XX/
      ele_ID_attempt_XX/
        routing.json
        *.raw.h5
        stim_times.txt
        stim_events.csv
        trial_manifest.csv
        trial_journal.jsonl
        trial_metrics.csv
        probe_metrics.json
        raster.{png,pdf,svg}
    probe_summary.{csv,png,pdf,svg}
    probe_unit_summary.{json,csv}
    selected_formal_sites.yaml
  raw_data/spatiotemporal/
    block_meta.yaml
    condition_manifest.csv
    01_pre_spont/*.raw.h5
    02_stim/
      *.raw.h5
      stim_times.txt
      stim_events.csv
      trial_manifest.csv
      trial_journal.jsonl
      segment_time_meta.json
    03_post_spont/*.raw.h5
  experiment_result.json
```

一个正式刺激H5包含全部交叉条件和5min休息。拆分分析必须用 `trial_manifest.csv` 的condition、stage和event_ids，不能把一个文件视作单条件。全方案10200个事件时刻：单电极300、同步300（同一事件时刻双电极同时脉冲）、时序9600。

每个脉冲在同一Sequence中插入合法key-value Event。同步有一个事件、两个DAC同时更新；时序分别有第一和第二事件，用DelaySamples精确控制dt，扣除第一脉冲600µs波形时长。不同trial使用唯一event_id。

文件关闭后，以本H5首帧为原点读取events导出stim_times.txt；txt、spikes及trial_manifest使用同一时间坐标。时序manifest以第二事件作为后续3–50ms分析锚点。run级墙钟CSV只作审计。主机估计与事件差>50ms写WARNING；缺事件、重复事件或实际dt不符则停止，不把估计时刻当已验证刺激。

每次main创建全新的时间目录；相同秒目录已存在时明确报错。逐trial命令记录立即写journal并fsync；每段刺激结束立即导出段内表。失败保留journal及失败状态，停止刺激、先stop_recording、等待写盘、再stop_file，断开并关闭stim unit、DAC回中性。若硬件收尾失败单独记录cleanup_failure.json。不会自动向不完整H5尾部续写，也不会自动绕过失败阶段开始正式刺激。

Probe逐位点路由时，如果SDK确认connect成功而刺激单元查询为空，该位点保存为`status=unroutable`、`all_trials=0`、`eligible=false`；没有发送刺激，也没有该位点的H5，响应及伪迹指标为null（图表显示N/A）。随后继续原四电极unit中其余候选，不替换邻居。unit报告区分原候选、实际测试和不可路由电极，仍要求至少3个unit各有2个有效位点。SDK错误回复、非法单元编号、映射变化及正式多电极路由失败继续停止。旧版中途失败的run保留已完成数据；更新代码后重新启动会建立新run并从头Probe，不应把重新启动当作断点续测。

Probe最后一次刺激后保持录制开启，默认等待1.0秒，随后才调用stop_recording；停止录制后的写盘等待仍独立执行。现场旧版100ms尾部出现raw有约90–100ms覆盖但最后spike仅到刺激后约3–7ms的现象，延迟处理/保存是待验证原因。可在stimulation.yaml的probe下设置`recording_tail_sec`（至少0.5秒），旧配置缺省时自动使用1.0秒并记入快照及位点指标，不修改原YAML。延长等待不能补回旧文件缺失的spikes，必须现场确认末次响应窗口的spike记录。

`maxwell.event_threshold`是SDK在线spike检测阈值，单位为相对于均值的标准差倍数，不是Probe有效响应阈值；模板8.5未经本culture标定。手动基线及Probe的spike计数比较前，需要核对H5 settings中的spike_threshold、gain、hpf、sampling是否一致。其他实验的5.5不能直接照搬，也不能仅改变Probe检测阈值而继续使用检测设置不同的旧基线。记录采样率从H5读取；Sequence的50µs命令步长是独立概念，不用其他项目的sample_us字段覆盖设备采样率。

只读核对已有run的录制设置，不读取raw或spikes，不连接设备：

```bash
python -B scripts/inspect_recording_settings.py --run data/20261011_133020_data
```

## 可选的unit内交替Probe

按用户2026-10-11确认，`--probe-order unit_interleaved`采用每0.5秒一个单电极脉冲，A→B→C→D循环10轮，每个位点10次。2Hz指全局发刺激频率；每个位点间隔2秒，实际为0.5Hz。默认仍为sequential逐位点10次；这是明确的新Probe节奏，不宣称每个位点保持原2Hz。400mV、每相300µs、先负后正、10–50ms全阵列响应、同宽去burst背景和严格7/10判定保持不变。

先运行以下路由检查。它加载原CFG并检查所有原四电极unit，准备的stim unit保持断开；不创建或下发Sequence，不记录H5：

```bash
python -B main.py --mode hardware --enable-hardware --headless \
  --probe-routing-check --probe-order unit_interleaved
```

查看新run的`pre_experiment/probe_routing_summary.csv`或json。四个位点必须获得互不相同的刺激单元，记录映射必须保持不变。共享单元/明确空查询保留为无法交替，不替换原邻居；通信错误或映射变化仍停止。少于3个unit能独立交替时不会开始这批Probe。旧SDK的download及MaxOne offset各需要5秒等待，因此无法靠每0.5秒重新download单电极CFG维持此交替节奏；必须在固定四电极池内快切。

完成录制设置和路由复核后，真实交替Probe独立启动：

```bash
python -B main.py --mode hardware --enable-hardware --headless \
  --probe-only --probe-order unit_interleaved
```

第一发刺激前再次检查全部unit路由。每个可路由unit保存一份40次交替刺激的H5；`trial_manifest.csv`记录电极、轮次和H5事件时间，独立的`ele_ID_metrics/trial_metrics.csv`每份10次，路径写入`probe_summary.csv`的`trial_metrics_path`。不可测试unit保持原候选列表，标记not_tested。每个unit最后一发后继续录制默认1秒，再正常关闭H5。不会自动开始正式实验，也不会从旧失败run续写。

交替Probe结果如果经过人工复核满足正式标准，后续独立正式命令同样需带`--probe-order unit_interleaved --formal-from <已复核Probe目录>`，以校验Probe定义一致；还会核对检测阈值和gain。真实可路由性、快切延迟和末次spike完整性需要现场确认，软件模拟不能代替。

完整raw记录很大：默认after_second调度下，正式阶段名义约176.9min；1024通道、20kHz、uint16未压缩估算约435GB，实际取决于映射通道数、压缩和路由/构建耗时。请配置足够空间的数据盘。保留raw是为了后续第一脉冲污染与同步伪迹排查。

## 与Markdown及方案的对应

- `experiment_template.md`：独立项目、单入口、YAML、run归档、三段录制、Event和段内时间表。
- `data_template.md`：CFG微米坐标、H5映射校验、全局帧换算与run审计表。
- `stim_pool_routing_limits.md`：unique stim units、DAC0/1、仅prev/current快切；settle wait默认50ms只是初始值，需要用现场测量值替换，未做真实A/B验证。
- `pre_experiment_automation.md`：离线完成burst、完整网络、出入度、空间互斥候选及低出度参数复核记录，再人工审阅中心输入；不自动衔接硬件。Probe响应定义按本次方案覆盖默认3–50ms/2倍局部基线规则，保留严格>60%接续门槛。
- `analysis_template.md`：Probe保存0–2ms均值及通用>=200标记、0–3ms计数及>300参考标记；按用户2026-10-11明确要求，伪迹只用于送达排查，成功判定仅使用10–50ms群体响应。正式数据检查pipeline按用户要求暂缓。
- `coding_style.md`与`paper.md`：结果自动输出、参数快照、日志与进度、600dpi PNG及矢量图；无标题和背景网格。

公共burst检测直接复用既有 `spont_burst_outdegree_handoff/spont_analysis/burst.py`，文件内容不改动。基线导入也可使用人工已复核的start_sec,end_sec CSV，避免强行使用不适合当前culture的默认burst参数。公共接口索引见 `analysis/analysis_utils.md`。

## 软件检查与范围

```bash
python -m unittest discover -s tests -v
```

软件检查覆盖调度计数、交叉顺序、两种时序方向、DAC极性、精确dt、严格7/10门槛、unit排名、时间原点及失败门控。模拟运行不导入maxlab、不连接设备，其H5带synthetic属性，没有真实raw电压。

未进行真实刺激、Linux静态库链接、硬件路由/settling现场验收或实验科学结论验证。自发基线的稳定延时Map用于实验前选点，已实现；正式刺激响应中的强响应概率、稳定延时Map、Consistency、ESRC和第一刺激污染验证按用户要求暂缓。
