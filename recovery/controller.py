#控制器，检测到训练停止后重新启动训练

from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen

import shutil
import subprocess
import sys
from pathlib import Path

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from prometheus_client import Counter, start_http_server


TRAINING_DIR = Path(r"E:\codex\Prometheus\checkpoint_demo")
TRAINING_SCRIPT = TRAINING_DIR / "auto_resume.py"


#节点切换，当node-a训练停止时，controller会切换到node-b继续训练
NODE_DIRS = {
    "node-a": TRAINING_DIR,
    "node-b": Path(r"E:\codex\Prometheus\migration_demo\node_b"),
}

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
    global training_process, active_node

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

    node_dir = NODE_DIRS[node_name]
    #筛选节点训练脚本路径
    training_script = node_dir / "auto_resume.py"

    if not training_script.exists():
        print(f"Training script not found: {training_script}")
        return

    #启动训练进程
    training_process = subprocess.Popen(
        [sys.executable, str(training_script)],
        cwd=node_dir,
    )

    active_node = node_name
    recovery_starts_total.inc()
    print(
        f"Training process started on {node_name}, "
        f"pid={training_process.pid}"
    )


class AlertHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return

        self.send_error(404)

    def do_POST(self):
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
                    
                    #如果 Checkpoint 复制成功，则在目标节点启动训练进程
                    if copy_checkpoint(source_node, target_node):
                         start_training(target_node)
                    else:
                        print("Migration aborted because Checkpoint copy failed")

                    break
        
        elif payload.get("status") == "resolved":
            for alert in payload.get("alerts", []):
                labels = alert.get("labels", {})

                if labels.get("alertname") == "TrainingExporterDown":
                    recovery_success_total.inc()
                    print("Training recovery verified")
                    print("Prometheus can scrape the training process again")
                    break
        
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"received")

    def log_message(self, format, *args):
        return


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
