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

## 备注

Linux 上也可以用 `--network host` 替代 `--add-host`，容器直接共享宿主机网络，`host.docker.internal` 换成 `127.0.0.1`。但这样会与 Windows 的启动方式产生更多差异，不利于双设备同步，本项目统一使用 `--add-host` 方案。
