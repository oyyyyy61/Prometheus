# Prometheus 异构加速卡汇总

## 当前部署方式

当前 Kubernetes 集群中的 Prometheus 可以直接访问三个 exporter 入口：

| 主机 | 设备 | Exporter | 地址 |
| --- | --- | --- | --- |
| `rental-910b2` | 2 x Ascend 910B2 | MindX NPU exporter | `192.168.1.50:18082` |
| `lab-501` | 1 x RTX 4090 | DCGM exporter | `10.1.125.178:9400` |
| `qujing` | 1 x NVIDIA GB10 | DCGM exporter | `10.252.28.0:9400` |

因此推荐让中央 Prometheus 直接抓取 exporter，再用 recording rules 统一指标：

```text
MindX NPU exporter ----\
RTX 4090 DCGM ----------> 中央 Prometheus -> accelerator:* -> Grafana
GB10 DCGM -------------/
```

厂商差异不会影响 Prometheus 汇总。原始指标的名字和语义存在差异，
`k8s/accelerator-federation-rules.yaml` 将它们统一成以下序列：

- `accelerator:utilization_percent`
- `accelerator:temperature_celsius`
- `accelerator:power_watts`
- `accelerator:memory_used_bytes`
- `accelerator:memory_total_bytes`
- `accelerator:memory_utilization_percent`
- `accelerator:device_count`
- `accelerator:exporter_up`

每张卡保留 `host`、`accelerator_vendor`、`accelerator_model` 和
`accelerator_id` 标签。可用下面的 PromQL 验证四张卡：

```promql
accelerator:utilization_percent
```

全局平均利用率和总功耗：

```promql
avg(accelerator:utilization_percent)
sum(accelerator:power_watts)
```

按厂商汇总：

```promql
avg by (accelerator_vendor) (accelerator:utilization_percent)
sum by (accelerator_vendor) (accelerator:power_watts)
```

部署并检查：

```bash
kubectl apply -k k8s
kubectl apply -k grafana
kubectl -n monitoring get scrapeconfig,prometheusrule
```

Grafana 中打开“异构加速卡联邦总览”。当前 GB10 的 DCGM exporter 没有暴露
`DCGM_FI_DEV_FB_USED`、`DCGM_FI_DEV_FB_FREE` 和
`DCGM_FI_DEV_FB_RESERVED`，因此其显存图没有数据；其他三张卡仍可聚合显存。

## 何时使用 `/federate`

只有每台远端设备已经各自运行 Prometheus，或中央 Prometheus 无法直接访问
exporter 时，才需要 Prometheus 的层级联邦：

```text
远端 Prometheus A --\
远端 Prometheus B ----> 中央 Prometheus /federate 抓取
远端 Prometheus C --/
```

`/federate` 是 Prometheus 服务端接口，不能直接配置成 node_exporter、DCGM
exporter 或 NPU exporter 的地址。中央 Prometheus Operator 可使用以下
`ScrapeConfig`；将 targets 换成三个远端 Prometheus 的 `9090` 地址，并给远端
Prometheus 配置唯一的 `external_labels.cluster`：

```yaml
apiVersion: monitoring.coreos.com/v1alpha1
kind: ScrapeConfig
metadata:
  name: edge-prometheus-federation
  namespace: monitoring
  labels:
    release: monitoring
spec:
  metricsPath: /federate
  honorLabels: true
  params:
    match[]:
      - '{__name__=~"accelerator:.*"}'
      - 'up{component=~"gpu|npu"}'
  staticConfigs:
    - targets:
        - 'edge-ascend.example:9090'
        - 'edge-lab501.example:9090'
        - 'edge-qujing.example:9090'
```

联邦入口需要网络访问控制或反向代理认证，避免把 Prometheus 查询接口直接暴露
到不可信网络。边缘 Prometheus 保留原始高频数据，中央实例只拉取 recording
rules 和必要的健康指标，可以控制带宽和序列数量。

## 能力边界

这个方案联合的是监控数据。让 Ascend 与 NVIDIA 共同执行同一个训练或推理任务，
还需要训练框架、通信后端和调度器支持；Prometheus 不参与设备计算或任务切分。
