# NPU 聚合规则与 Agent 内存 Exporter

这个目录包含两部分：

- `prometheus-rules.yaml`：Prometheus Operator 使用的 `PrometheusRule`，计算 HBM 总量、已用、剩余、使用率、进程占比和数据新鲜度。
- `agent_memory_exporter.py`：嵌入推理运行时的低基数 exporter，输出 Agent 生命周期、KV Cache 逻辑/物理内存、共享内存、可回收内存、命中/未命中、重算和迁移指标。
- `agent_memory_client.py`：独立 Agent 进程使用的本地事件客户端，把生命周期和 KV 状态发送给 bridge。
- `vllm_agent_bridge.py`：运行在远端 NPU 机器的捕获 bridge，读取 `session_router /routing-stats`、vLLM `/metrics` 和 NPU exporter `/metrics`，在 Agent 业务埋点接入前提供聚合数据。

## 部署聚合规则

当前 Kubernetes 集群使用 Prometheus Operator，可以直接执行：

```bash
kubectl apply -k npu
```

规则要求 Prometheus 选择 `release=monitoring` 的 `PrometheusRule`。应用后检查：

```bash
kubectl -n monitoring get prometheusrule npu-aggregate-recording-rules
curl -s 'http://127.0.0.1:9090/api/v1/query?query=npu:hbm_utilization_percent' | jq
```

当前 NPU exporter 的 HBM 指标按 MiB 处理，recording rule 名称保留 `_mib`，避免把单位混入计算时丢失。

如果使用独立 Prometheus 容器，把 `recording_rules.yml` 加入 `rule_files`，并把该文件挂载到 Prometheus 容器内。

## 接入推理运行时

```python
from npu.agent_memory_exporter import AgentMemoryExporter

exporter = AgentMemoryExporter(
    runtime="vllm-ascend",
    model="your-model",
    device="npu-4",
)
exporter.start(port=8001)

exporter.upsert_agent(
    "internal-agent-id",
    state="active",
    stage="decode",
    context_tokens=8192,
    branch_count=1,
)
exporter.set_memory(
    "internal-agent-id",
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

`agent_id` 只保存在进程内，用于更新记录和计算聚合值，不会成为 Prometheus 标签。Branch、Block 的详细关系建议写入 trace 或结构化日志；Prometheus 保存聚合结果。

运行时 Pod 需要添加：

```yaml
metadata:
  labels:
    agent-memory-exporter: enabled
spec:
  containers:
    - name: inference
      ports:
        - name: agent-metrics
          containerPort: 8001
```

`agent-memory-podmonitor.yaml` 会自动发现带有这个标签的 Pod，并抓取 `/metrics`。

## 远端 vLLM 捕获 bridge

当前远端运行结构可以直接使用 bridge：

```text
session_router :8000/routing-stats
vLLM           :8001/metrics
NPU exporter   :8082/metrics
bridge         :8002/metrics
Agent events   :8003/agent-events (仅远端回环地址)
```

把 `agent_memory_exporter.py` 和 `vllm_agent_bridge.py` 同步到远端 NPU 容器，例如
`/data/Agentrix/observability/`，然后在远端启动：

```bash
cd /data/Agentrix/observability
python vllm_agent_bridge.py \
  --port 8002 \
  --control-port 8003 \
  --router-url http://127.0.0.1:8000/routing-stats \
  --vllm-url http://127.0.0.1:8001/metrics \
  --npu-url http://127.0.0.1:8082/metrics \
  --model Qwen3.5-9B
```

先在远端验证：

```bash
curl -sS http://127.0.0.1:8002/metrics | grep -E 'agent_|vllm-agent'
```

bridge 第一阶段提供：

- `/routing-stats` 中的 Session/rank 聚合；
- vLLM running/waiting 请求数；
- vLLM KV Cache 使用率；
- Prefix Cache hit/miss 增量；
- prompt/generation token 增量；
- KV 物理容量的 NPU 侧估算。

### 在独立 Agent 进程中上报

当前 bridge 负责创建 exporter。Agent 业务进程通过本机 `8003` 端口发送事件，避免
每个请求创建一套 Prometheus exporter。把 `agent_memory_client.py` 放进 Agent 运行环境：

```python
from agent_memory_client import AgentMemoryClient

metrics = AgentMemoryClient("http://127.0.0.1:8003/agent-events")

# 请求创建、进入 Prefill/Decode
metrics.upsert_agent(
    request_id,
    state="active",
    stage="prefill",
    context_tokens=prompt_tokens,
    branch_count=1,
)

# 从 vLLM block manager 或 Agent KV manager 读取真实 block 统计
metrics.set_memory(
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

# 调用工具时开始/结束等待
metrics.set_state(request_id, state="tool_wait", stage="tool_wait")
metrics.upsert_agent(request_id, state="tool_wait", stage="tool_wait", tool_wait_seconds=wait_seconds)
metrics.set_state(request_id, state="active", stage="decode")

# KV 在 NPU 与主存之间迁移
metrics.record_transfer(
    "offload", bytes_count=bytes_count, duration_seconds=duration,
    source_tier="npu", target_tier="host",
)
metrics.record_transfer(
    "restore", bytes_count=bytes_count, duration_seconds=duration,
    source_tier="host", target_tier="npu",
)

# KV 缺失导致重算，以及请求结束
metrics.record_recompute(recomputed_tokens)
metrics.remove_agent(request_id)
```

调用位置建议固定为：请求创建/销毁处调用 `upsert_agent`、`remove_agent`；Prefill、Decode、
Tool Call 状态切换处调用 `set_state`；KV block manager 完成分配、共享、释放或迁移后调用
`set_memory`/`record_transfer`；检测到 prefix miss 或 KV 重算后调用 `record_recompute`。
`logical_bytes`、`physical_bytes`、`shared_bytes`、`reclaimable_bytes` 和 `block_count`
必须来自实际 KV manager，不能用 HBM 总量代替。

## 本地验证

```bash
cd npu
python -m unittest -v test_agent_memory_exporter.py
python agent_memory_exporter.py --port 8001
```
