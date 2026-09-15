# Kubernetes 运行说明

以下命令在本目录的上一级项目根目录执行。示例使用本机 HTTP registry；也可以把两个镜像改成你的镜像仓库地址。

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

ConfigMap 更新后执行 `kubectl -n training-demo rollout restart deployment/training-demo`，让 Pod 重新读取脚本。

## 验证和故障注入

```bash
kubectl -n training-demo get pods -o wide
kubectl -n training-demo logs deploy/training-recovery-controller -f
kubectl -n training-demo delete pod -l app=training-demo
kubectl -n training-demo get pods -w
```

Deployment 会创建替代 Pod；替代 Pod 从 `/checkpoints/auto_checkpoint.pt` 恢复训练。单节点实验使用 `hostPath`，多节点部署时请替换为 RWX PVC 或网络文件系统。
