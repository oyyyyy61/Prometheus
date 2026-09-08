#控制器，检测到训练停止后重新启动训练

from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen


import subprocess
import sys
from pathlib import Path

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from prometheus_client import Counter, start_http_server


TRAINING_DIR = Path(r"E:\codex\Prometheus\checkpoint_demo")
TRAINING_SCRIPT = TRAINING_DIR / "auto_resume.py"

training_process = None

recovery_starts_total = Counter(
    "recovery_starts_total",
    "Number of training processes started by recovery controller",
)


PROMETHEUS_QUERY_URL = "http://127.0.0.1:9090/api/v1/query"


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


def start_training():
    global training_process

    if training_process is not None and training_process.poll() is None:
        print("Training process is already running")
        return

    training_process = subprocess.Popen(
        [sys.executable, str(TRAINING_SCRIPT)],
        cwd=TRAINING_DIR,
    )
    recovery_starts_total.inc()
    print("Metrics endpoint: http://127.0.0.1:9001/metrics")
    print(f"Training process started, pid={training_process.pid}")


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

                if labels.get("alertname") == "TrainingExporterDown":
                    print("TrainingExporterDown detected")
                    start_training()
                    break
        
        elif payload.get("status") == "resolved":
            for alert in payload.get("alerts", []):
                labels = alert.get("labels", {})

                if labels.get("alertname") == "TrainingExporterDown":
                    print("Training recovery verified")
                    print("Prometheus can scrape the training process again")
                    break
        
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"received")

    def log_message(self, format, *args):
        return


server = HTTPServer(("0.0.0.0", 9000), AlertHandler)

print("Recovery controller listening on port 9000")
print("Health endpoint: http://127.0.0.1:9000/healthz")
print("Webhook endpoint: http://127.0.0.1:9000/alert")
training_is_up = get_training_status()
print("Prometheus reports training up =", training_is_up)


try:
    server.serve_forever()
except KeyboardInterrupt:
    print("\nRecovery controller stopped")
finally:
    server.server_close()