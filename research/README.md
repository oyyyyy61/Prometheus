# 预算化主动观测与 Telemetry-aware Safe Control

这个目录包含研究用的、与现有恢复控制器隔离的参考实现。当前原型不连接真实 Kubernetes API，也不直接改变现有训练恢复流程，目标是先用回放数据验证策略行为。

## 研究假设

1. 在相同观测预算下，按诊断不确定性和候选动作需求选择观测，可以保留更多决策价值。
2. telemetry 完整性应进入控制状态。当指标缺失、冲突或结构退化时，控制器应降低动作强度或暂停动作。
3. 动作必须绑定可检查的后验条件。验证数据缺失时，结果按失败处理。

## 原型组成

`budgeted_control.py` 提供四个纯函数式组件：

- `BudgetedObservationPolicy`：按观测价值/成本和候选动作需求选择 L0、L1、L2 观测。
- `TelemetryAwareSafeControl`：根据 telemetry 状态、证据集合、诊断置信度和动作风险输出 `ACT`、`ESCALATE` 或 `ABSTAIN`。
- `verify_action`：用动作前后指标检查后验条件；指标缺失时 fail closed。
- `default_observations`：与当前 Prometheus 原型对应的最小信号目录。

## 当前信号层级

```text
L0  gpu_utilization, hbm_occupancy, queue_latency, scrape_freshness
L1  cpu_network, workflow_phase, kv_recompute
L2  nccl_collective, pcie_nvlink, short_trace
```

## 运行单元测试

在 `research/` 目录执行：

```bash
python -m unittest -v test_budgeted_control.py
```

## 下一步实验接口

后续应添加一个 JSONL replay runner，每行包含：

```json
{
  "timestamp": 0,
  "metrics": {"gpu_utilization": 0.2, "queue_latency": 1.8},
  "telemetry": {"scrape_freshness": 1.0, "sample_loss": 0.0},
  "budget": 8.0,
  "action_candidates": ["batch_adjustment", "reroute"]
}
```

实验输出至少记录：观测成本、选择的信号、诊断状态、控制决策、动作后验证结果、错误动作次数和恢复时间。这样可以比较固定全量采集、固定低粒度采集、预算化观测以及预算化观测加安全控制四类策略。

当前已提供 `replay.py`，可直接运行：

```bash
python replay.py samples.jsonl --output decisions.jsonl
```

其中 `diagnosis` 字段可选；缺少诊断时只评估观测策略，加入诊断字段后会同时评估安全控制门控。

## 从真实 Prometheus 生成快照

训练 exporter 和 Prometheus 已经启动时，在 `research/` 目录执行：

```bash
python prometheus_adapter.py \
  --prometheus-url http://127.0.0.1:9090/api/v1/query \
  --budget 8 \
  --actions batch_adjustment,reroute \
  --count 10 \
  --interval 5 \
  --output training_snapshots.jsonl
```

adapter 会查询当前项目已有的 `training_*` 和 `up{job="training"}` 指标，生成以下内容：

```text
metrics：原始训练进度、loss、Checkpoint 时间戳、保存耗时、文件大小、恢复次数
telemetry：scrape_freshness、sample_loss、time_series_gaps、sensor_conflicts
adapter_errors：没有样本或 HTTP 查询失败的指标及原因
```

`checkpoint_age_seconds` 由当前时间减去最近一次成功保存 Checkpoint 的时间得到。它可以把“训练进程仍然存活，但 Checkpoint 长时间不更新”暴露给后续控制策略。

## 生成四类故障回放

下面的命令会生成 4 个场景、每个场景 8 个时间点的 JSONL：

```bash
python generate_fault_replays.py \
  --all \
  --steps 8 \
  --budget 8 \
  --output fault_replays.jsonl
```

四类场景的含义如下：

```text
cpu_io：GPU 利用率下降，同时 CPU 利用率、IO wait 或 tool wait 上升
memory_pressure：HBM、KV recompute、cache miss 变差
communication：NCCL collective p99、rank skew、PCIe/NVLink 压力上升
telemetry_failure：GPU 指标消失、scrape freshness 降低、样本大量丢失
```

然后运行策略回放：

```bash
python replay.py fault_replays.jsonl --output fault_decisions.jsonl
```

生成器只模拟观测结果，不会启动训练进程，不会修改 Kubernetes，也不会调用现有 recovery controller。
