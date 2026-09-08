# AI 集群动态恢复学习项目

这个目录用于逐步学习和实现基于 Prometheus、Kubernetes 与训练框架的故障监控和动态恢复。

## 当前阶段：普通 Checkpoint 恢复

当前先完成动态恢复的最小基础能力：

```text
训练模型
→ 周期性保存 Checkpoint
→ 手动终止训练程序
→ 重新启动程序
→ 自动读取 Checkpoint
→ 从已保存进度继续训练
```

实验位于 [`checkpoint_demo`](./checkpoint_demo)。进入该目录后按照其中的 `README.md` 操作。

## 学习路线

1. 普通 Checkpoint 保存和恢复。
2. 捕获故障预警并触发紧急 Checkpoint。
3. 使用 Prometheus 采集训练进度和 Checkpoint 状态。
4. 使用 Alertmanager 发送故障告警。
5. 使用 Kubernetes Controller 定位作业并执行恢复。
6. 支持节点迁移和弹性缩容、扩容。

## 当前目录结构

```text
Prometheus/
├── README.md
└── checkpoint_demo/
    ├── README.md
    └── train.py
```
