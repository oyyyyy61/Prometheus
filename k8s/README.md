# Kubernetes 运行说明

以下命令在本目录的上一级项目根目录执行。示例使用本机 HTTP registry；也可以把两个镜像改成你的镜像仓库地址。

## 准备共享存储

`storage.yaml` 是 NFS 静态 PV 示例。部署前请把其中的 `server` 和 `path` 改成实际值，并确认所有 Kubernetes 节点都能访问该 NFS 目录。

```bash
sudo mkdir -p /srv/prometheus-checkpoints
sudo cp shared_storage/job-1/auto_checkpoint.pt /srv/prometheus-checkpoints/
```

## 构建并推送实验镜像

```bash
docker build -f k8s/runtime.Dockerfile -t 127.0.0.1:5000/training-runtime:v1 k8s
docker push 127.0.0.1:5000/training-runtime:v1
docker build -f k8s/controller.Dockerfile -t 127.0.0.1:5000/training-recovery-controller:v1 .
docker push 127.0.0.1:5000/training-recovery-controller:v1
```

## 创建脚本 ConfigMap

```bash
kubectl -n training-demo create configmap training-script \
  --from-file=auto_resume.py=checkpoint_demo/auto_resume.py \
  --dry-run=client -o yaml | kubectl apply -f -
```

## 部署

```bash
kubectl apply -k k8s
```

执行 `kubectl apply -k k8s` 前需要先创建 `training-script` ConfigMap；更新脚本后重新执行创建命令并滚动重启训练 Deployment。

## 双节点迁移测试

先确认至少两个节点处于 Ready 状态：

```bash
kubectl get nodes -o wide
kubectl -n training-demo get pod -o wide
```

记录训练 Pod 当前所在节点后，对该节点执行维护模拟：

```bash
kubectl cordon <当前节点名>
kubectl drain <当前节点名> --ignore-daemonsets --delete-emptydir-data
```

观察 Pod 是否迁移到另一节点，并确认 Checkpoint 仍可读取：

```bash
kubectl -n training-demo get pod -o wide -w
kubectl -n training-demo logs -l app=training-demo --tail=50
```

恢复节点调度：

```bash
kubectl uncordon <当前节点名>
```

测试结束后可以清理实验资源：

```bash
kubectl delete -k k8s
```

ConfigMap 更新后执行 `kubectl -n training-demo rollout restart deployment/training-demo`，让 Pod 重新读取脚本。

## 验证和故障注入

```bash
kubectl -n training-demo get pods -o wide
kubectl -n training-demo logs deploy/training-recovery-controller -f
kubectl -n training-demo delete pod -l app=training-demo
kubectl -n training-demo get pods -w
```

Deployment 会创建替代 Pod；替代 Pod 从 `/checkpoints/auto_checkpoint.pt` 恢复训练。单节点实验使用 `hostPath`，多节点部署时请替换为 RWX PVC 或网络文件系统。
