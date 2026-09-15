# AI 集群动态恢复学习项目

这个目录用于逐步学习和实现基于 Prometheus、Kubernetes 与训练框架的故障监控和动态恢复。

## 当前阶段：Kubernetes 语义下的 Checkpoint 恢复

当前先完成动态恢复的最小基础能力：

```text
训练模型
→ 周期性保存 Checkpoint
→ 手动终止训练程序
→ 重新启动程序
→ 自动读取 Checkpoint
→ 从已保存进度继续训练
```

基础实验位于 [`checkpoint_demo`](./checkpoint_demo)。Kubernetes 部署文件位于 [`k8s`](./k8s)，按照其中的 `README.md` 可构建镜像、创建 ConfigMap、部署训练工作负载和恢复控制器。

## 学习路线

1. 普通 Checkpoint 保存和恢复。
2. 捕获故障预警并触发紧急 Checkpoint。
3. 使用 Prometheus 采集训练进度和 Checkpoint 状态。
4. 使用 Alertmanager 发送故障告警。
5. 使用 Kubernetes Controller 定位作业并执行恢复。
6. 支持节点迁移和弹性缩容、扩容。

当前 Kubernetes 闭环包括：Deployment 维持训练 Pod，`recovery/k8s_controller.py` 通过 LIST/WATCH 发现失败 Pod 并删除卡死实例，替代 Pod 从共享 Checkpoint 恢复；PodMonitor 和 PrometheusRule 负责指标采集与告警。

## 当前目录结构

```text
Prometheus/
├── README.md
├── checkpoint_demo/
├── migration_demo/
├── recovery/
├── shared_storage/
├── monitoring/
└── k8s/
    ├── README.md
    ├── namespace.yaml
    ├── deployment.yaml
    ├── controller-deployment.yaml
    ├── controller-rbac.yaml
    ├── podmonitor.yaml
    ├── prometheusrule.yaml
    ├── kustomization.yaml
    └── *.Dockerfile
```
