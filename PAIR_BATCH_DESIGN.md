# 两对一批的正式刺激方案

版本入口仍为`run_formal.py`，不新建培养物目录，也不更改Probe或Windows离线分析。
文件顶部修改当前配置目录、正式选点和调度参数；公开版本没有预选培养物或真实电极。

## 默认设计

- 三个unit，每unit两点，共6个不同刺激电极，仍须通过全部6点的独立刺激单元联合路由检查。
- 保留原6种单点、6种同步组合；时序dt为1、5、10、50、100 ms，双方向，每条件50次。
- 初始6个单点交错，各50次，共300次。
- 原6个无序电极对组成3批，每批一个跨unit组合与不共享位点的本地组合。
  例如跨unit1a–2a与本地3a–3b；另两批同理。批次顺序由种子确定。
- 每批50轮，每轮先后测试两对；两对先后顺序每轮互换。
  每对小块含1种同步和5个dt双方向，共11次，块内用固定种子打乱。
  这样每种主条件均衡覆盖该批前、中、后。
- 每批pre、mid、post各测4个刺激点，每点10次单点参照；mid位于第25轮后。
  每次参照先在4点间交错，不能将其视作额外50次主条件样本。
- 最后一个完整双相脉冲结束后静息1秒，再切换并等待稳定；短块之间不增加5分钟休息。
- 只在批与批之间休息300秒，共2次；最后一批直接进入后自发，不加额外末阶段休息。
- 前、后各600秒自发；全程只存spikes、事件和元数据。

主体3600次（单点300、同步300、时序3000），参照360次，总3960次。
以50ms切换稳定等待、5ms序列初始延时计算，默认名义时长约101.3分钟。
这不是硬件完成时限承诺：初始化、路由、文件关闭、传输、暂停或阻塞send都可能延长。
trial静息增加1秒，3960次会增加约66分钟，不能把2秒静息仍当作两小时内。
每对首末跨度由计划逐对报告；保留同步和中点参照后约20分钟，不沿用纯时序18分钟的估算。

## 编辑与运行

在现有`run_formal.py`顶部填写CONFIG_DIR和SELECTION；选点仍使用原有可审计Probe来源或显式人工理由。
请使用当前培养物，不能复制本项目以前的位点或成绩。SCHEDULE_MODE默认pair_batches，
DT_MS、REFERENCE_TRIALS、SCHEDULE_SEED、BATCH_REST_SEC和INTER_SEQUENCE_REST_SEC也集中在顶部。
修改仅在内存中生效，YAML不覆盖；实际快照写入本轮结果。

```bash
conda activate maxlab
python -B run_formal.py
```

默认只预演，不连接设备。预演计划打印总trial、主体/参照数、时长、每对首末跨度。
原有`--routing-check --enable-hardware`仍是只检查联合路由；真实正式启动仍需手动明确使用
`python -B run_formal.py --enable-hardware`，不会自动运行Probe或衔接正式采集。
SCHEDULE_MODE设legacy可回到原调度；此时dt和休息沿用原YAML，公开默认录制模式仍为spikes_events。

## 保存与分析身份

- 计划：experiment_plan.json、formal_condition_plan.csv、formal_schedule_plan.csv。
- 实际：raw_data/spatiotemporal/schedule_manifest.csv、condition_manifest.csv、batch_schedule.json。
- 原来的前自发、刺激、后自发三段H5继续保留；刺激事件仍以H5帧对齐。
- trial_manifest.csv增加batch_id、pair_id、checkpoint、schedule_index；不能以主机预计时间替代H5事件。
- 漂移参照使用独立条件名`batch_X_ref_pre/mid/post_<site-label>`与relation=drift_reference。
  离线Level1可以单独计算这些参照，但10次参照主要用于漂移观察，不能混入主条件50次或声称同等精度。
- 每轮随机种子、全部计划顺序与实际顺序均保存；批次比较依旧可能受时间/历史影响。
  换点不代表全网已休息，1秒静息是否适合当前培养物仍需现场检查。
- 固定6点联合路由、最后脉冲/响应窗/静息结束前不切换等护栏不变。

## Git存储与更新

新方案位于同一仓库的`paired-batch-design`分支，以`additional-probe-six-of-ten`的已核验提交为起点。
保留旧分支，不复制成第二个实验项目，不向main合并。
该次提交只包含通用Python代码、模拟测试及本说明；不提交H5、CFG、培养物YAML或现场选点。
原项目data下的每次运行继续独立保存快照、选点来源和执行计划；Git分支不代替数据归档。

Linux切换前先`git status --short`，保留你编辑过的入口和配置。
工作区干净时可`git fetch origin`，再`git switch --track origin/paired-batch-design`。
已有同名本地分支时使用`git switch paired-batch-design`与`git pull --ff-only`。
若Git报告本地修改冲突，停下检查差异，不reset、不强制覆盖现场配置。

软件测试使用人工模拟电极/H5，无硬件；完整检查结果见PAIR_BATCH_CHECKS.md。
