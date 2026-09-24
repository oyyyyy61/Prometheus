# 远端 Agent 业务源码监控接入说明

本文说明如何把远端 Agent 业务源码接入当前 Prometheus 监控系统。

当前监控链路如下：

```text
远端 Agent 业务进程
        |
        | HTTP POST 事件
        v
Agent bridge :8003/agent-events
        |
        | 聚合为 Prometheus 指标
        v
Agent bridge :8002/metrics
        |
        v
本地 Prometheus -> Recording Rules -> Grafana
```

## 一、需要接入的组件

把下面文件放到远端 Agent 运行环境中：

```text
npu/agent_memory_client.py
```

远端 bridge 需要使用以下文件：

```text
npu/agent_memory_exporter.py
npu/vllm_agent_bridge.py
```

其中：

- `agent_memory_client.py`：Agent 业务进程使用的事件上报客户端。
- `agent_memory_exporter.py`：接收事件，并把业务状态聚合成 Prometheus 指标。
- `vllm_agent_bridge.py`：同时采集路由器、vLLM、NPU exporter，并提供事件接收端口。

Agent 业务进程与 bridge 分属两个 Python 进程时，使用 `AgentMemoryClient` 通过本机回环地址通信。Agent 进程与 exporter 位于同一个 Python 进程时，可以直接调用 `AgentMemoryExporter` 的方法。

## 二、启动远端 bridge

远端 bridge 使用两个端口：

```text
8002  Prometheus metrics 接口
8003  Agent 事件接收接口，仅监听 127.0.0.1
```

启动命令：

```bash
cd /data/Agentrix/observability
/data/Agentrix/.venv/bin/python vllm_agent_bridge.py \
  --port 8002 \
  --control-port 8003 \
  --control-address 127.0.0.1 \
  --router-url http://127.0.0.1:8000/routing-stats \
  --vllm-url http://127.0.0.1:8001/metrics \
  --npu-url http://127.0.0.1:8082/metrics \
  --model Qwen3.5-9B
```

检查 bridge 是否正常：

```bash
curl -sS http://127.0.0.1:8003/healthz
curl -sS http://127.0.0.1:8002/metrics | grep '^agent_'
```

健康检查应返回：

```json
{"status":"ok"}
```

## 三、在 Agent 入口接入生命周期

在 Agent 请求创建、状态切换和请求结束的位置创建客户端：

```python
from agent_memory_client import AgentMemoryClient

# 一个 Agent 进程只创建一个客户端实例，避免每个请求重复初始化连接配置。
agent_metrics = AgentMemoryClient(
    "http://127.0.0.1:8003/agent-events"
)
```

### 3.1 请求创建

插入位置：Agent 请求进入主循环、即将调用 vLLM 的位置。

监控内容：

- 当前 Agent 数量；
- Agent 当前状态；
- 当前阶段；
- 上下文 token 数；
- Branch 数量。

```python
agent_metrics.upsert_agent(
    request_id,
    state="active",
    stage="prefill",
    context_tokens=prompt_token_count,
    branch_count=1,
)
```

对应指标：

```text
agent_lifecycle_agents
agent_lifecycle_context_tokens
agent_lifecycle_branch_count
```

### 3.2 Prefill、Decode 和状态切换

插入位置：Agent 状态机或请求调度器发生阶段变化的位置。

支持的状态包括：

```text
active、tool_wait、shared、cold、offloading、offloaded、
restoring、reclaimable、released、unknown
```

支持的阶段包括：

```text
planning、prefill、decode、tool_call、tool_wait、merge、unknown
```

示例：

```python
agent_metrics.set_state(
    request_id,
    state="active",
    stage="decode",
)
```

## 四、在 Tool 调用路径接入等待时间

插入位置：调用工具前、工具返回后，以及工具异常退出的清理路径。

监控内容：

- Agent 是否处于 `tool_wait`；
- 工具等待累计时间；
- 工具等待发生在哪个工作阶段。

```python
import time

agent_metrics.set_state(
    request_id,
    state="tool_wait",
    stage="tool_wait",
)

wait_started = time.monotonic()
try:
    tool_result = call_tool(tool_name, tool_arguments)
finally:
    # exporter 保存的是当前累计等待时间，不是单次增量。
    total_wait_seconds += time.monotonic() - wait_started
    agent_metrics.upsert_agent(
        request_id,
        state="active",
        stage="decode",
        tool_wait_seconds=total_wait_seconds,
    )
```

对应指标：

```text
agent_lifecycle_tool_wait_seconds
agent:tool_wait_seconds:sum
```

## 五、在 KV block 管理路径接入内存数据

插入位置：实际管理 KV block 的代码中，例如：

- KV block 分配完成后；
- Prefix Cache 命中并建立共享关系后；
- Copy-on-Write 后；
- block 标记为可回收后；
- 请求释放 KV 后。

示例：

```python
agent_metrics.set_memory(
    request_id,
    memory_kind="kv_cache",
    state="active",
    tier="npu",
    logical_bytes=logical_bytes,
    physical_bytes=physical_bytes,
    shared_bytes=shared_bytes,
    reclaimable_bytes=reclaimable_bytes,
    block_count=block_count,
)
```

字段含义：

| 字段 | 监控含义 | 数据来源 |
| --- | --- | --- |
| `logical_bytes` | Agent 逻辑上持有的 KV 数据量 | Agent KV 账本或请求上下文 |
| `physical_bytes` | 实际占用的 NPU KV 数据量 | vLLM block manager |
| `shared_bytes` | 被多个 Agent/Branch 共享的数据量 | Prefix Cache 或共享 block 表 |
| `reclaimable_bytes` | 当前可回收的数据量 | KV 回收器或 block 状态表 |
| `block_count` | 使用的 KV block 数量 | vLLM block manager |

对应指标：

```text
agent_memory_bytes{memory_kind="kv_cache",view="logical"}
agent_memory_bytes{memory_kind="kv_cache",view="physical"}
agent_memory_bytes{memory_kind="kv_cache",view="shared"}
agent_memory_bytes{memory_kind="kv_cache",view="reclaimable"}
agent_memory_blocks{memory_kind="kv_cache"}
```

这些字段需要使用 KV manager 的真实数据。HBM 总容量、NPU HBM 已用量不能直接作为 KV block 统计值。

## 六、接入 KV 命中、未命中和重算

当前 bridge 已经从 vLLM 指标读取：

```text
vllm:prefix_cache_hits_total
vllm:prefix_cache_queries_total
```

因此常规 vLLM Prefix Cache 命中率无需在 Agent 代码中重复上报。bridge 会产生：

```text
agent_kv_cache_hits_total
agent_kv_cache_misses_total
agent_kv_cache_hit_ratio
agent:kv_hit_ratio:cumulative
```

如果 Agent 自己维护独立的 KV 命中计数器，可以发送累计值：

```python
agent_metrics.set_kv_totals(
    hits=kv_hits_total,
    misses=kv_misses_total,
)
```

发生 KV 缺失、被驱逐后重新计算或因共享失效导致 token 重算时：

```python
agent_metrics.record_recompute(recomputed_tokens)
```

对应指标：

```text
agent_kv_recompute_tokens_total
```

`set_kv_totals` 与 vLLM bridge 的计数来源需要二选一，避免同一批请求被重复计算。

## 七、接入 KV Offload / Restore

插入位置：真正执行数据复制的函数，而非仅仅改变 Agent 状态的位置。

### 7.1 Offload：NPU 到主存或外部存储

```python
started = time.monotonic()
try:
    move_kv_to_host(blocks)
finally:
    agent_metrics.record_transfer(
        "offload",
        bytes_count=transferred_bytes,
        duration_seconds=time.monotonic() - started,
        source_tier="npu",
        target_tier="host",
    )
```

### 7.2 Restore：主存或外部存储到 NPU

```python
started = time.monotonic()
try:
    move_kv_to_npu(blocks)
finally:
    agent_metrics.record_transfer(
        "restore",
        bytes_count=transferred_bytes,
        duration_seconds=time.monotonic() - started,
        source_tier="host",
        target_tier="npu",
    )
```

对应指标：

```text
agent_memory_offload_bytes_total
agent_memory_restore_bytes_total
agent_memory_transfer_duration_seconds_bucket
agent_memory_transfer_duration_seconds_sum
agent_memory_transfer_duration_seconds_count
```

`bytes_count` 应使用实际复制字节数，`duration_seconds` 应使用复制操作耗时。仅发生状态切换却没有数据复制时，不应记录迁移吞吐。

## 八、请求结束和异常清理

插入位置：正常返回、超时、取消和异常处理的共同清理路径。

```python
try:
    result = run_agent_request(request)
finally:
    agent_metrics.remove_agent(request_id)
```

如果 KV block 进入共享池或回收池，请先发送最终的 `set_memory` 状态，再删除 Agent：

```python
agent_metrics.set_memory(
    request_id,
    memory_kind="kv_cache",
    state="reclaimable",
    tier="npu",
    logical_bytes=logical_bytes,
    physical_bytes=physical_bytes,
    shared_bytes=shared_bytes,
    reclaimable_bytes=reclaimable_bytes,
    block_count=block_count,
)
agent_metrics.remove_agent(request_id)
```

## 九、源码中应该搜索的位置

在远端 Agent 项目根目录执行：

```bash
rg -n "async def|def .*generate|tool|agent|branch|session|request" .
rg -n "KV|kv_cache|block_manager|prefix_cache|paged|offload|restore|recompute" .
```

重点寻找以下函数：

```text
请求入口：create/run/generate/handle_request
状态切换：planning/prefill/decode/tool_call/tool_return
工具执行：call_tool/execute_tool/invoke_tool
KV 管理：allocate/free/share/copy/evict/reclaim
迁移操作：offload/restore/swap/migrate
异常清理：finally/cancel/timeout/release
```

## 十、部署和验证顺序

### 10.1 同步文件到远端

```bash
scp npu/agent_memory_client.py \
    root@REMOTE_HOST:/data/Agentrix/observability/

scp npu/agent_memory_exporter.py npu/vllm_agent_bridge.py \
    root@REMOTE_HOST:/data/Agentrix/observability/
```

### 10.2 重启 bridge

```bash
pkill -f '[v]llm_agent_bridge.py --port 8002' || true
cd /data/Agentrix/observability
/data/Agentrix/.venv/bin/python vllm_agent_bridge.py \
  --port 8002 \
  --control-port 8003 \
  --router-url http://127.0.0.1:8000/routing-stats \
  --vllm-url http://127.0.0.1:8001/metrics \
  --npu-url http://127.0.0.1:8082/metrics \
  --model Qwen3.5-9B
```

### 10.3 验证事件通道

```bash
curl -sS http://127.0.0.1:8003/healthz

curl -sS -X POST http://127.0.0.1:8003/agent-events \
  -H 'Content-Type: application/json' \
  -d '{"event":"upsert","agent_id":"manual-check","state":"active","stage":"decode","context_tokens":128,"branch_count":1}'

curl -sS http://127.0.0.1:8002/metrics | \
  grep -E 'agent_lifecycle_agents|agent_lifecycle_context_tokens'
```

验证完成后清理测试 Agent：

```bash
curl -sS -X POST http://127.0.0.1:8003/agent-events \
  -H 'Content-Type: application/json' \
  -d '{"event":"remove","agent_id":"manual-check"}'
```

### 10.4 验证 Prometheus

在本地 Prometheus 查询：

```promql
up{component="agent-runtime",host="rental-910b2"}
agent:kv_cache_physical_bytes:sum{host="rental-910b2"}
agent:memory_blocks:sum{host="rental-910b2"}
agent:tool_wait_seconds:sum{host="rental-910b2"}
rate(agent_memory_offload_bytes_total{host="rental-910b2"}[5m])
rate(agent_kv_recompute_tokens_total{host="rental-910b2"}[5m])
```

## 十一、数据解释

| 现象 | 解释 |
| --- | --- |
| `agent_lifecycle_agents` 为 0 | 当前没有通过事件通道登记的 Agent，或请求已经清理 |
| `agent_memory_bytes` 全为 0 | Agent 没有上报 KV manager 的 block 数据 |
| `agent_memory_offload_bytes_total` 为 0 | 观察窗口内没有发生实际 offload |
| `agent_lifecycle_tool_wait_seconds` 为 0 | 没有 Tool Wait，或工具路径还没有埋点 |
| `agent_kv_recompute_tokens_total` 为 0 | 没有发生 KV 重算 |
| NPU HBM 有占用、算力为 0 | NPU 保留了运行时内存，采样时没有执行计算任务 |

事件上报接口只监听远端 `127.0.0.1`，不会直接暴露到公网。Prometheus 只读取 `8002/metrics`，不调用 `8003/agent-events`。

## 十二、背景设计文档提出的数据需求

`agentrix_memory_control_plane_background.md` 是设计背景和需求依据，重点讨论长程 Agent 的资源归因、生命周期、共享关系、迁移过程和后续受控管理能力。它描述的是需要建立的观测模型，当前 exporter 已实现的字段只覆盖其中一部分。

背景文档中的“训练进程”在当前远端环境应理解为实际运行 Agent、vLLM 和 KV manager 的业务进程。若进程执行的是纯模型训练任务，还需要按第十五节补充训练专用字段。

### 12.1 必须从远端业务进程读取的数据

| 数据类别 | 需要读取的数据 | 主要来源 | 作用 |
| --- | --- | --- | --- |
| 资源身份 | `agent_id`、`session_id`、`branch_id`、`workflow_stage`、`model`、`device_id` | Agent 上下文、路由器、运行时 | 将设备资源归属到 Agent 执行结构 |
| 生命周期 | 创建、活跃、Prefill、Decode、Tool Call、Tool Wait、恢复、释放、异常结束 | Agent 状态机 | 解释内存为什么继续驻留 |
| Agent 工作量 | 上下文 token、Prompt token、Generation token、并发请求、等待请求 | Agent 和 vLLM | 关联工作量、吞吐和内存变化 |
| 分支关系 | Branch 创建、父子关系、Branch 数、合并和终止原因 | Agent planner/workflow | 解释多分支带来的逻辑内存增长 |
| KV 逻辑视图 | 每个 Agent/Branch 逻辑持有的 KV 字节数 | Agent KV 账本 | 表达“每个分支需要多少历史” |
| KV 物理视图 | 实际占用的 block 数、物理字节数、所在层级 | vLLM block manager | 表达 NPU/CPU 的真实占用 |
| KV 共享关系 | 共享 block 数、共享字节数、引用计数、Copy-on-Write | Prefix Cache/block manager | 计算共享收益，避免重复统计 |
| KV 可回收性 | 可回收 block 数、可回收字节数、回收原因 | KV 回收器 | 判断释放空间和潜在重算代价 |
| 工具状态 | 工具名称或受限类别、开始时间、结束时间、等待时长、返回/异常状态 | Agent Tool executor | 计算 Tool Wait 对资源驻留的影响 |
| 迁移过程 | Offload/Restore 的字节数、源层级、目标层级、开始/结束时间、耗时、结果 | KV 迁移实现 | 计算吞吐、带宽竞争和恢复代价 |
| 重算过程 | 重算 token 数、触发原因、关联 Agent/Branch | KV miss/recompute 路径 | 衡量释放内存带来的计算代价 |
| 设备状态 | HBM 总量、已用量、剩余量、算力、HBM 带宽、功耗、采样时间 | NPU exporter/npu-smi | 判断设备压力和迁移动机 |

### 12.2 低基数指标与详细关系数据的分工

Prometheus 适合保存聚合后的时间序列。以下关系不应直接作为 Prometheus label：

```text
agent_id、session_id、branch_id、block_id、parent_branch_id
```

这些字段数量会随请求增长，直接放入 label 会导致时序数量快速膨胀。建议采用以下分工：

```text
Prometheus 指标：按 state、stage、tier、memory_kind 聚合的数量和字节数
结构化日志/Trace：Agent、Session、Branch、Shared Prefix、Block 的详细关系
事件存储：状态转移、迁移、回收、重算的时间线和原因
```

当前 `AgentMemoryExporter` 已经按低基数维度聚合：

```text
runtime、model、device、state、stage、memory_kind、tier、view
```

Agent 业务代码仍然需要将详细关联关系写入日志或 Trace，Prometheus 指标用于查询趋势、告警和聚合结果。

## 十三、从数据计算出的核心结果

### 13.1 逻辑内存与物理内存

同一统计范围内，读取：

```text
logical_bytes  = 逻辑上下文总量
physical_bytes = 实际 block 占用总量
shared_bytes   = 被多个消费者引用的物理数据量
```

可计算：

```text
共享收益字节数 = logical_bytes - physical_bytes
物理占用率     = physical_bytes / HBM_total_bytes
共享占比       = shared_bytes / physical_bytes
```

计算前需要保证三个字节数使用同一统计范围，避免把不同 Agent 或不同存储层混合相除。

### 13.2 可回收空间和回收代价

```text
reclaimable_ratio = reclaimable_bytes / physical_bytes
```

可回收数据还需要同时记录：

```text
是否存在主存/外部副本
释放后是否需要重算
预计重算 token 数
预计恢复耗时
```

只有引用关系已经解除、迁移状态已经完成、异步复制已经结束的 block，才可以标记为 `reclaimable`。最近未访问本身不足以证明资源可以安全回收。

### 13.3 Tool Wait 与内存白占

Agent 处于 `tool_wait` 时，应同时观察：

```text
tool_wait_seconds
physical_bytes
reclaimable_bytes
runtime_requests_running
```

这样可以识别以下情况：Agent 长时间等待工具，模型请求已经停止计算，但大量 KV 仍占用 NPU；或者等待状态下的 KV 已经安全迁移，可以降低设备压力。

### 13.4 Offload/Restore 代价

```text
offload_throughput = rate(agent_memory_offload_bytes_total[5m])
restore_throughput  = rate(agent_memory_restore_bytes_total[5m])
transfer_cost       = duration_seconds / bytes_count
```

迁移数据需要和以下指标对齐：

```text
NPU HBM 使用率
NPU HBM 带宽利用率
Tool Wait 时间
请求延迟
KV 重算 token 数
```

只有同时观察释放空间和恢复/重算代价，才能判断迁移策略是否有效。

### 13.5 生命周期时间线

至少记录以下事件顺序：

```text
create -> planning -> prefill -> decode -> tool_call -> tool_wait
       -> restore/offload -> decode -> merge -> release
```

Prometheus 记录阶段聚合值，完整的开始时间、结束时间、耗时和异常原因应写入结构化事件。例如：

```json
{
  "event": "tool_wait_end",
  "agent_id": "request-123",
  "session_id": "session-9",
  "branch_id": "branch-2",
  "duration_seconds": 4.8,
  "physical_bytes": 734003200,
  "reclaimable_bytes": 524288000,
  "timestamp": 1790150400.12
}
```

## 十四、需要从已有 exporter 读取的字段

Agent 业务埋点不应重复采集已经由 vLLM/NPU exporter 提供的设备级数据。bridge 当前读取：

### vLLM `/metrics`

```text
vllm:num_requests_running
vllm:num_requests_waiting
vllm:kv_cache_usage_perc
vllm:prefix_cache_hits_total
vllm:prefix_cache_queries_total
vllm:prompt_tokens_total
vllm:generation_tokens_total
vllm:num_preemptions_total
vllm:cache_config_info
```

用途：

- 当前运行和排队请求数；
- KV 当前使用率；
- Prefix Cache 累计命中率；
- Prompt/Generation token 吞吐；
- 抢占次数和 KV offload 配置；
- vLLM engine 与 NPU 的基础关联。

### NPU exporter `/metrics`

```text
npu_chip_info_hbm_total_memory
npu_chip_info_hbm_used_memory
npu_chip_info_hbm_utilization
npu_chip_info_overall_utilization
npu_chip_info_power
```

bridge 另外使用 `npu-smi` 补充：

```text
AICore utilization
AICube utilization
AIVector utilization
HBM bandwidth utilization
```

这些字段描述设备状态，不能替代 Agent 级的逻辑 KV、共享 KV、Tool Wait 和可回收 KV 数据。

## 十五、纯训练进程的补充字段

背景文档的核心对象是长程 Agent 推理。如果远端实际运行的是纯训练任务，需要单独补充训练语义，避免把训练 step 当作 Agent lifecycle：

```text
training_job_id
model_name
rank、world_size、local_device_id
epoch、global_step、micro_step
batch_size、sequence_length、input_tokens、target_tokens
forward_seconds、backward_seconds、optimizer_seconds
data_loading_wait_seconds
checkpoint_save_seconds、checkpoint_size_bytes、checkpoint_status
```

训练任务还应保留：

```text
训练进程 PID 和命令摘要
CPU/主存占用
NPU HBM 占用和算力
通信流量或通信等待
故障、重启和恢复事件
```

这些字段可以使用单独的 `training_*` 指标命名空间，和 `agent_*` 指标分开。当前 `AGENT_RUNTIME_INTEGRATION.md` 的生命周期、KV block、Tool Wait 和 Offload/Restore 章节适用于 Agent/vLLM 推理路径。

## 十六、当前实现与背景需求的差距

当前已经具备：

- NPU HBM、算力、HBM 带宽和数据新鲜度采集；
- vLLM 请求、KV 使用率、Prefix Cache 命中率采集；
- Agent 生命周期事件接收接口；
- KV 逻辑/物理/共享/可回收字节数接口；
- KV block 数量接口；
- Tool Wait、重算、Offload、Restore 事件接口；
- Prometheus recording rules 和 Grafana 面板。

仍需从远端 Agent/vLLM 源码补充：

- Agent 与 Session、Branch 的详细关联；
- Prefix Sharing 和 Copy-on-Write 的真实引用关系；
- block manager 的真实 block 数和字节数；
- 可回收判定依据和释放后的重算代价；
- Tool Wait 的真实开始/结束时间；
- Offload/Restore 的实际复制字节数和耗时；
- 结构化事件时间线和异常原因；
- 纯训练任务的 step、epoch、checkpoint 和数据加载指标。

本阶段的接入目标是先把远端业务事实上报完整，再由 Prometheus 进行聚合计算。人工释放、强制迁移和自动策略控制需要等引用关系、恢复代价和安全边界验证完成后再实现。
