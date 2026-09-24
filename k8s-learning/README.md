# Kubernetes 学习区

这个目录用于 Kubernetes 原理学习、命令练习和可重复实验。

仓库中的 `k8s/` 目录继续服务于现有的信息监控系统，包含监控控制器、Prometheus 采集配置、告警规则和相关部署清单。学习实验优先放在本目录，避免影响现有监控配置。

## 建议目录

```text
k8s-learning/
├── README.md
├── notes/          # 原理笔记
├── manifests/      # 学习用 YAML
├── labs/           # 可重复实验
└── troubleshooting/ # 故障排查记录
```

## 学习顺序

1. 集群架构、API、控制器和声明式管理
2. Pod 生命周期与容器配置
3. Deployment、Service、ConfigMap、Secret
4. 调度、资源管理和健康检查
5. 网络、Ingress、DNS 和 NetworkPolicy
6. PV、PVC 和 StorageClass
7. RBAC、ServiceAccount 和安全策略
8. Helm、Kustomize 与发布运维
9. Prometheus Operator 与 Kubernetes 可观测性

## 实验约定

- 学习资源使用独立 Namespace，默认命名为 `k8s-learning`。
- YAML 文件先在本目录维护，再通过 `kubectl apply --dry-run=client` 检查。
- 涉及现有监控系统的变更，继续放在原有目录并单独确认影响范围。
