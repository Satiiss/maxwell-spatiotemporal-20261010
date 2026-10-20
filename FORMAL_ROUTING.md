# 原位点联合软件路由准备

此工具尝试保留ActivityScan所选的全部物理记录电极，为明确指定的刺激位点重新计算路由。
只在mxwserver内创建随机唯一名字的软件Array，不initialize、activate、download、offset、
不配置StimulationUnit/DAC/Sequence、不录制或刺激；不会改变当前芯片配置。
需要Linux上的官方maxlab SDK及运行中的mxwserver，不需要重新做ActivityScan。
准备期间不要同时运行会改变来源CFG或使用服务器的其他配置任务。

## 使用

编辑`scripts/prepare_formal_routing.py`顶部的CFG_PATH和STIMULATION_ELECTRODES。
公开入口默认留空，不包含实际实验路径、电极编号或培养物标签。
运行前填写本轮CFG_PATH与STIMULATION_ELECTRODES，不引用或改写旧Probe成绩。
以后换盘只改这两个字段；MAX_ATTEMPTS为有界尝试次数（默认4，最多16）。

在实验项目根目录固定运行：

```bash
conda activate maxlab
python -B scripts/prepare_formal_routing.py
```

这不是正式实验启动命令。没有硬件启用参数，也不会接续Probe或正式实验。

## 检查与结果

先在独立软件Array中加载原CFG，清理旧刺激链接，联合连接全部指定电极后重新查询所有分配。
查询发生在整池连接完成后，防止后连电极改变先连位点的分配而漏检。
若原CFG已独立可用，停止重路由并报告`original_cfg_already_independent`。

否则调用官方select_electrodes、select_stimulation_electrodes、route。
SDK仅提供优先级等公开接口，本工具依次尝试有限种确定的输入顺序，不指定任意stim_unit，
不保证顺序变化一定产生不同路由，也不保证能找到解。
任何记录电极缺失、新增或坐标变化的候选都拒绝，不靠减少记录位点或替换邻近电极过关。
只换记录通道可以保留为待复核候选，报告逐电极变更列表。
每个候选先写盘，再用新的软件Array重载，按正式程序相同的清理/联合连接路径检查，
要求所有指定电极都有不同的0–31刺激单元，且刺激连接没有改变记录映射。

结果另存到`data/<时间>_formal_routing/`，包含日志，以及`candidate/`中的：

- `routing_preparation.json`：来源哈希、全部尝试、联合分配、映射变更、状态和软件/硬件边界。
- `joint_pool.csv`：通过的联合分配；未通过时仅有表头。
- `attempt_XX.cfg`：各次软件计算的产物，可能不合格，不应直接用于实验。
- `candidate_formal.cfg`：仅全部软件检查通过时生成；不覆盖原CFG。

`no_solution_found`仅表示这些有界尝试未找到解，不能证明硬件上不可能存在其他路线。
SDK返回错误时停止并记录failed，不把传输错误当作可用结果。

## 后续边界

软件通过不等于实际硬件独立连接或刺激送达，也不改变既有Probe的生物成绩。
因此它只能尝试恢复原三个unit的物理位点组合，不能让选点自动达到原方案的生物有效性要求。
当前工具不会下载候选、启用候选或改入口/YAML；无需现在重新做实验。

以后决定采用新CFG时，先另行核验硬件联合路由，再确认基线映射/采集设置并做新CFG下的Probe。
`baseline_mapping_reusable`只表示CFG映射完全相同，不是H5已核对、也不保证旧基线仍适合当时培养物状态。
旧Probe来源CFG哈希校验保留，不能修改哈希/绕过来源校验来复用旧成绩。
正式流程的固定池独立检查、快切和最后脉冲结束后静息护栏保持原样。

官方依据：https://api-docs.mxwbio.com/tutorial_index.html
