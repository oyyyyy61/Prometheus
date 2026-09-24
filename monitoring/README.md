# 监控组件部署说明

Prometheus 和 Alertmanager 通过 Docker 运行，配置文件（`prometheus.yml`、`alerts.yml`、`alertmanager.yml`）由 git 同步，两台设备共用同一份，不需要修改。

配置中容器访问宿主机统一使用 `host.docker.internal`：

- **Windows（Docker Desktop）**：自动解析，无需额外参数。
- **Linux（原生 Docker）**：默认不解析，启动容器时必须加 `--add-host=host.docker.internal:host-gateway`（Docker 20.10+ 支持，已在本机 Docker 29 上验证，解析到宿主机 `172.17.0.1`）。

## Linux 启动命令

在项目根目录执行：

```bash
docker run -d --name prometheus \
  -p 9090:9090 \
  --add-host=host.docker.internal:host-gateway \
  -v "$(pwd)/monitoring/prometheus.yml:/etc/prometheus/prometheus.yml" \
  -v "$(pwd)/monitoring/alerts.yml:/etc/prometheus/alerts.yml" \
  prom/prometheus

docker run -d --name alertmanager \
  -p 9093:9093 \
  --add-host=host.docker.internal:host-gateway \
  -v "$(pwd)/monitoring/alertmanager.yml:/etc/alertmanager/alertmanager.yml" \
  prom/alertmanager
```

Alertmanager 继续把 webhook 发给本机 `recovery/controller.py`。该 relay 通过
`REMOTE_RECOVERY_URL` 和 `REMOTE_RECOVERY_TOKEN` 把带有 `recovery_action` 标签的
Ascend 告警转发到远端 agent。启动 relay 时至少设置：

```bash
REMOTE_RECOVERY_URL=http://192.168.1.50:18004 \
REMOTE_RECOVERY_TOKEN='与远端 agent 相同的随机长 token' \
python recovery/controller.py
```

## Windows 启动命令（PowerShell）

```powershell
docker run -d --name prometheus `
  -p 9090:9090 `
  -v "${PWD}\monitoring\prometheus.yml:/etc/prometheus/prometheus.yml" `
  -v "${PWD}\monitoring\alerts.yml:/etc/prometheus/alerts.yml" `
  prom/prometheus

docker run -d --name alertmanager `
  -p 9093:9093 `
  -v "${PWD}\monitoring\alertmanager.yml:/etc/alertmanager/alertmanager.yml" `
  prom/alertmanager
```

## 验证

```bash
# Prometheus 能抓到训练进程（先在宿主机启动 auto_resume.py）
curl -s "http://127.0.0.1:9090/api/v1/query?query=up{job=\"training\"}"

# Alertmanager 收到告警后应推送到宿主机 9000 端口的 recovery controller
curl -s http://127.0.0.1:9093/api/v2/status
```

## 当前采集与告警

`10.1.125.178:9100` 提供 lab-501 主机的 CPU、内存、磁盘和网络指标，
`10.1.125.178:9400` 提供 RTX 4090 的 DCGM 指标，
`10.1.125.178:8000` 应由 4090 上实际运行的训练程序提供训练与 Checkpoint 指标。
当前已配置：

- GPU exporter、node exporter 不可用告警；
- GPU 温度高于 85°C、显存高于 95%、功耗高于 430W 告警；
- lab-501 文件系统使用率高于 90% 告警；
- 训练进程、训练进度、数据加载等待、Checkpoint 停滞和保存耗时告警。
- 远端 Ascend recovery agent、bridge、vLLM 队列停滞和 active Checkpoint 缺失告警；
  其中后三类会分别请求 `restart_bridge`、`restart_vllm` 和 `restore_checkpoint`。

训练 exporter 会暴露 `training_active`、`training_current_epoch`、
`training_samples_total`、`training_samples_per_second`、
`training_data_loading_wait_seconds`、`training_step_duration_seconds`、
`training_checkpoint_status` 等指标。`training_samples_total` 使用
`rate(training_samples_total[5m])` 可以得到一段时间内的平均吞吐量。

4090 Dashboard 的训练查询固定筛选 `host="lab-501",component="training"`，
不会读取本机或 Kubernetes `training-demo` 的同名指标。在 4090 主机启动实际训练任务后，
先确认 exporter 监听所有网络接口：

```bash
ss -lntp | grep ':8000'
curl -sS http://127.0.0.1:8000/metrics | grep '^training_'
```

再从 Prometheus 所在主机确认网络可达：

```bash
curl -sS http://10.1.125.178:8000/metrics | grep '^training_'
```

如果实际训练代码尚未导出这些指标，需要把 `prometheus_client` 的 Gauge/Counter
更新逻辑接入训练循环与真实 Checkpoint 保存成功的位置。DCGM 指标只能判断 GPU
是否繁忙，无法提供训练 epoch、保存耗时、快照路径或快照有效性。

更新 Kubernetes 中的训练脚本后，重新创建 ConfigMap 并滚动重启 Deployment：

```bash
kubectl -n training-demo create configmap training-script \
  --from-file=auto_resume.py=checkpoint_demo/auto_resume.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n training-demo rollout restart deployment/training-demo
```

当前示例的数据已经在内存中，数据等待值会接近零；接入真实 `DataLoader` 后，
同一个指标会反映磁盘读取、网络存储和 CPU 预处理等待。

远端恢复 agent 的部署文件位于 `recovery/`：`remote-agent.service` 使用独立的
`agentrix-recovery` 用户，`agentrix-recovery.sudoers.example` 只允许重启两个固定
systemd unit，`recovery-agent.env.example` 保存 token 和 checkpoint 路径。agent
默认只监听远端回环地址，`scripts/start-monitoring.sh` 通过 SSH 建立本地
`18004 -> 8004` 控制隧道和 `19004 -> 9004` 指标隧道。启动脚本要求设置
`REMOTE_RECOVERY_TOKEN`，并会把 `recovery/remote_agent.py` 同步到远端 observability
目录。

远端启用 systemd unit 的步骤：

```bash
sudo install -o root -g root -m 0644 recovery/remote-agent.service /etc/systemd/system/remote-agent.service
sudo install -d -m 0750 /etc/agentrix
sudo install -m 0600 recovery/recovery-agent.env.example /etc/agentrix/recovery-agent.env
sudo install -o root -g root -m 0440 recovery/agentrix-recovery.sudoers.example /etc/sudoers.d/agentrix-recovery
sudo visudo -cf /etc/sudoers.d/agentrix-recovery
sudo systemctl daemon-reload
sudo systemctl enable --now remote-agent.service
```

将 env 示例中的 token 和路径改成实际值，再在监控机设置同一个
`REMOTE_RECOVERY_TOKEN`。启动脚本默认只重启这个受限 unit；开发调试可显式设置
`REMOTE_RECOVERY_USE_SYSTEMD=0` 使用临时进程模式。

手动验证三个受限动作（控制隧道建立后执行）：

```bash
curl -H "Authorization: Bearer $REMOTE_RECOVERY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"action":"restart_bridge"}' http://192.168.1.50:18004/v1/actions
curl -H "Authorization: Bearer $REMOTE_RECOVERY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"action":"restart_vllm"}' http://192.168.1.50:18004/v1/actions
curl -H "Authorization: Bearer $REMOTE_RECOVERY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"action":"restore_checkpoint"}' http://192.168.1.50:18004/v1/actions
```

## 备注

Linux 上也可以用 `--network host` 替代 `--add-host`，容器直接共享宿主机网络，`host.docker.internal` 换成 `127.0.0.1`。但这样会与 Windows 的启动方式产生更多差异，不利于双设备同步，本项目统一使用 `--add-host` 方案。
