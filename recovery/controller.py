#控制器，检测到训练停止后重新启动训练

from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen

import os
import shutil
import subprocess
import sys
from pathlib import Path

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from prometheus_client import Counter, start_http_server


TRAINING_DIR = Path(r"E:\codex\Prometheus\checkpoint_demo")
TRAINING_SCRIPT = TRAINING_DIR / "auto_resume.py"
recovery_in_progress = False

#节点切换，当node-a训练停止时，controller会切换到node-b继续训练
NODE_DIRS = {
    "node-a": TRAINING_DIR,
    "node-b": Path(r"E:\codex\Prometheus\migration_demo\node_b"),
}

#共享 Checkpoint 路径
SHARED_CHECKPOINT_PATH = Path(
    r"E:\codex\Prometheus\shared_storage\job-1\auto_checkpoint.pt"
)

#控制器状态文件路径
CONTROLLER_STATE_PATH = (
    SHARED_CHECKPOINT_PATH.parent / "controller_state.json"
)

CHECKPOINT_NAME = "auto_checkpoint.pt"
active_node = "node-a"

training_process = None

recovery_starts_total = Counter(
    "recovery_starts_total",
    "Number of training processes started by recovery controller",
)

recovery_success_total = Counter(
    "recovery_success_total",
    "Number of training recoveries verified by Alertmanager resolved events",
)


PROMETHEUS_QUERY_URL = "http://127.0.0.1:9090/api/v1/query"

#得到训练进程的状态，返回 True 表示训练进程正在运行，False 表示训练进程已经停止
def get_training_status():

    #把 up{job="training"} 转换成可放进 URL 的格式
    query_string = urlencode({
        "query": 'up{job="training"}',
    })
    request_url = f"{PROMETHEUS_QUERY_URL}?{query_string}"

    try:
        #urlopen访问 Prometheus HTTP API  timeout=3：Prometheus 三秒无响应就结束等待
        with urlopen(request_url, timeout=3) as response:
            #把 Prometheus 返回的 JSON 转成 Python 字典
            payload = json.load(response)
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        print("Failed to query Prometheus:", error)
        return None

    results = payload.get("data", {}).get("result", [])

    if not results:
        print("Prometheus returned no training target")
        #Prometheus 查询失败或没有这项指标
        return None

    #取出查询结果中的 0 或 1
    value = float(results[0]["value"][1])
    return value == 1.0

#检查训练进程的 metrics 端点是否可访问，返回 True 表示可访问，False 表示不可访问
def training_endpoint_is_reachable():
    try:
        with urlopen("http://127.0.0.1:8000/metrics", timeout=1) as response:
            return response.getcode() == 200
    except (URLError, TimeoutError, OSError):
        return False

#找到训练进程的 checkpoint 文件，并复制到另一个节点
def copy_checkpoint(source_node, target_node):
    if source_node not in NODE_DIRS:
        print(f"Unknown source node: {source_node}")
        return False

    if target_node not in NODE_DIRS:
        print(f"Unknown target node: {target_node}")
        return False

    source_path = NODE_DIRS[source_node] / CHECKPOINT_NAME
    target_path = NODE_DIRS[target_node] / CHECKPOINT_NAME
    temporary_path = target_path.with_suffix(target_path.suffix + ".tmp")

    if not source_path.exists():
        print(f"Checkpoint not found: {source_path}")
        return False

    shutil.copy2(source_path, temporary_path)
    temporary_path.replace(target_path)

    print(f"Checkpoint copied from {source_node} to {target_node}")
    return True

#开始训练进程
def start_training(node_name=None):
    global training_process, active_node, recovery_in_progress

    if node_name is None:
        node_name = active_node

    #判断当前node是否在已知节点列表中
    if node_name not in NODE_DIRS:
        print(f"Unknown node: {node_name}")
        return
    #判断训练进程是否已经在运行
    if training_process is not None and training_process.poll() is None:
        print("Training process is already running")
        return

    #判断训练进程的 metrics 端点是否可访问，如果可访问则说明训练进程已经在运行，避免重复启动
    if training_endpoint_is_reachable():
        print("Training endpoint is already reachable; skip duplicate start")
        return
    
    node_dir = NODE_DIRS[node_name]
    #筛选节点训练脚本路径
    training_script = node_dir / "auto_resume.py"

    if not training_script.exists():
        print(f"Training script not found: {training_script}")
        return

    #设置环境变量 TRAINING_CHECKPOINT_PATH，指向共享 Checkpoint 路径
    training_environment = os.environ.copy()
    training_environment["TRAINING_CHECKPOINT_PATH"] = str(
        SHARED_CHECKPOINT_PATH
    )

    #启动训练进程
    training_process = subprocess.Popen(
        [sys.executable, str(training_script)],
        cwd=node_dir,
        env=training_environment,
    )

    recovery_in_progress = True

    active_node = node_name
    save_active_node(active_node)
    recovery_starts_total.inc()
    print(
        f"Training process started on {node_name}, "
        f"pid={training_process.pid}"
    )

#加载当前活跃的节点，如果控制器状态文件不存在或无效，则默认返回 node-a
def load_active_node():
    if not CONTROLLER_STATE_PATH.exists():
        return "node-a"

    try:
        state = json.loads(
            CONTROLLER_STATE_PATH.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return "node-a"

    node_name = state.get("active_node")

    if node_name not in NODE_DIRS:
        return "node-a"

    return node_name

#保存当前活跃的节点到控制器状态文件
def save_active_node(node_name):
    temporary_path = CONTROLLER_STATE_PATH.with_suffix(".json.tmp")

    temporary_path.write_text(
        json.dumps({"active_node": node_name}),
        encoding="utf-8",
    )
    temporary_path.replace(CONTROLLER_STATE_PATH)


class AlertHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return

        self.send_error(404)

    def do_POST(self):
        global recovery_in_progress
        if self.path != "/alert":
            self.send_error(404)
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)

        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(400, "invalid json")
            return

        print("\nAlertmanager webhook received")
        print(json.dumps(payload, ensure_ascii=False, indent=2))

        if payload.get("status") == "firing":
            for alert in payload.get("alerts", []):
                labels = alert.get("labels", {})

                #如果 Prometheus 报告训练进程停止，controller 会尝试切换到另一个节点继续训练
                if labels.get("alertname") == "TrainingExporterDown":
                    source_node = active_node
                    target_node = "node-b" if source_node == "node-a" else "node-a"

                    print(f"TrainingExporterDown detected on {source_node}")
                    print(f"Preparing migration to {target_node}")
                    
                    #如果共享 Checkpoint 存在，则使用共享 Checkpoint 启动训练，否则迁移中止
                    if SHARED_CHECKPOINT_PATH.exists():
                        print(f"Using shared Checkpoint: {SHARED_CHECKPOINT_PATH}")
                        start_training(target_node)
                    else:
                        print(f"Shared Checkpoint not found: {SHARED_CHECKPOINT_PATH}")
                        print("Migration aborted")

                    break
        
        elif payload.get("status") == "resolved":
            for alert in payload.get("alerts", []):
                labels = alert.get("labels", {})

                #如果 Prometheus 报告训练进程恢复，controller 会记录恢复成功的指标
                if labels.get("alertname") == "TrainingExporterDown":
                    if recovery_in_progress:
                        recovery_success_total.inc()
                        recovery_in_progress = False
                        print("Training recovery verified")
                        print("Prometheus can scrape the training process again")
                    else:
                        print("Training was already running; no recovery was initiated")
                    break
        
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"received")

    def log_message(self, format, *args):
        return

active_node = load_active_node()
print("Loaded active node =", active_node)
server = HTTPServer(("0.0.0.0", 9000), AlertHandler)

start_http_server(9001)

print("Recovery controller listening on port 9000")
print("Health endpoint: http://127.0.0.1:9000/healthz")
print("Webhook endpoint: http://127.0.0.1:9000/alert")
print("Metrics endpoint: http://127.0.0.1:9001/metrics")

training_is_up = get_training_status()
print("Prometheus reports training up =", training_is_up)

if training_is_up is False:
    print("Training is down at controller startup; starting recovery")
    start_training()

try:
    server.serve_forever()
except KeyboardInterrupt:
    print("\nRecovery controller stopped")
finally:
    server.server_close()
