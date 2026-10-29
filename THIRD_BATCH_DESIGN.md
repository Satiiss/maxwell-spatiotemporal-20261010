# 第三批：分时段时差实验

本分支从 `paired-batch-design` 的 `9f9ce313eacd76567124002db6eec2fe277cf937` 派生。
第三批代码使用独立入口，不覆盖第二批入口，不自动启动下一时段或由Probe接续正式刺激。
本次发布仅实现采集调度；尚未切换 stable_response_latency_standard.md 的首次spike分析口径。

## 参数与流程

编辑 `run_formal_third.py` 顶部常量和 `SELECTION`：

- `SESSION = "short"`：5、10 ms；`"long"`：50、100 ms。每次启动只执行选定一组。
- `CONFIG_DIR` 指向本轮采集配置；`SELECTION` 填本轮培养物与三个双电极unit及Probe证据。
  未填写实际参数和选点时入口会报错，不会使用占位电极启动采集。
- `TRIALS_PER_CONDITION = 50`：所有单点/参照、同步、时序条件均相同次数。
- `INTER_SEQUENCE_REST_SEC = 1.0`：末脉冲完整结束后静息，再切换并稳定；不是第一脉冲到下一trial间隔。
- `REST_AFTER_SYNC_SEC = 300`，`REST_BETWEEN_DT_SEC = 0`，`FINAL_REST_SEC = 300`。
- `PRE_SPONTANEOUS_SEC = POST_SPONTANEOUS_SEC = 600`。

流程为：前自发10分钟 → 初始六点单点S0 → 六对同步 → 休息5分钟 →
完整单点R0 → 第一个dt → 完整单点R1 → 第二个dt → 完整单点R2 →
休息5分钟 → 后自发10分钟。

三个unit各两个位点，六对组合为三个unit内组合及三个unit的a点两两组合。
单点每轮六点各一次，重复50轮，每组300 trial；同步六对各50次，共300 trial；
每个dt六对双方向各50次，共600 trial。每阶段cyclic轮转，先完成一个dt再进入另一个。
R0/R1对应第一个dt前后，R1/R2对应第二个dt前后。全部参照与S0使用相同条件、次数和轮转顺序。
若 `REST_BETWEEN_DT_SEC > 0`，在R1后休息，随后自动补一整组单点，再进入第二个dt。

默认每时段2700 trial（主体1800、参照900），不含Probe；short名义约77.66分钟，long约79.01分钟，
包括前后自发、两次5分钟休息和0.25秒刺激段尾部。初始化、文件关闭等另计。
加入时差间5分钟休息后，每时段3000 trial，增加约10.28分钟。
默认波形继承本轮配置，历史基准为400 mV、每相300 µs、先负后正、相间隔0。
实际幅值/采集参数以本轮快照为准；真实送达间隔以H5事件核验，软件测试不证明生物恢复充分或硬件疑点已修复。

## Linux固定启动命令

先激活设备环境 `conda activate maxlab`。在第三批独立项目目录内操作：

```bash
python run_formal_third.py
```

上面仅预演，不构造硬件对象、不发刺激、不生成H5。完成参数填写后可以单独检查联合路由：

```bash
python run_formal_third.py --routing-check --enable-hardware
```

路由检查只连接与检查六点路由，不发刺激、不采集H5。用户手动启动正式采集：

```bash
python run_formal_third.py --enable-hardware
```

short完成后程序退出。次晨手动把顶部 `SESSION` 改为 `"long"`，更新实际日期/DIV和配置证据后再预演及启动。
Probe继续使用独立 `run_probe.py`；本轮实际基线、CFG、路由与选点需核对，未预填旧培养物。

## 下载、更新与复现

Linux建议另建同级第三批目录，不在手改过的旧目录直接pull或切分支：

```bash
git clone --branch third-batch-dt-sessions https://github.com/Satiiss/maxwell-spatiotemporal-20261010.git spatiotemporal_third_batch
cd spatiotemporal_third_batch
git rev-parse HEAD
```

每次正式run已保存入口源码、配置快照、CFG及计划。另在实验记录保存启动前 `git rev-parse HEAD`；
顶部参数手改时也记录 `git diff -- run_formal_third.py config`。以后更新前先检查并保留本地改动，
不要用强制覆盖命令丢弃自己的参数。第二批分支保留，不自动合并main；完整硬件验证后再考虑合并。

## 输出与离线验证

每次run有三段H5：前自发、刺激、后自发；刺激段连续记录阶段间休息。
计划/结果保存session_id、dt、完整调度步骤（包括休息）、主体/参照数量及预算。
`formal_condition_plan.csv`、`formal_schedule_plan.csv` 为预期；实际 `trial_manifest.csv` 记录
session_id、checkpoint、stage、轮次、刺激条件、H5事件对齐时刻，`stim_times.txt` 使用本段H5时间轴。
即使参照刺激条件相同，也用不同名称和checkpoint分别留存，避免合并为一个静态基准。

回归检查：

```bash
python -m unittest discover -s tests -p 'test_dt_sessions.py' -v
python -m unittest discover -s tests -p 'test_pair_batches.py' -v
python -m unittest discover -s tests -p 'test_formal_timing.py' -v
```

新增5项检查覆盖两组全部条件/方向/轮转、参照身份、预算、休息后补参照、错误配置拒绝、
预演不构造硬件对象，以及两组缩小版模拟H5、事件和trial对应、Probe证据只读。
第二批9项与现有时序6项检查一并回归。尚未在MaxLab硬件执行；不上传原始数据、H5、CFG或实际培养物参数。
