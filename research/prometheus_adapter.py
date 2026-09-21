"""把 Prometheus 指标转换成研究用的 JSONL replay snapshot。

这个文件只做两件事：

1. 通过 Prometheus HTTP API 查询训练 exporter 已经暴露的指标；
2. 把查询结果整理成 replay.py 能够消费的统一 JSON 结构。

当前仓库里的训练程序没有 GPU utilization、HBM 或 NCCL 指标，所以这里
只转换真实存在的训练/Checkpoint 指标，并把缺失指标明确记录到 telemetry
字段中。后续接入 GPU exporter 时，可以在 METRIC_QUERIES 中继续增加查询。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


# 这些查询名是 replay snapshot 内部使用的稳定字段名。
# 右侧 PromQL 则对应当前项目已经存在的 exporter 指标。
METRIC_QUERIES: Mapping[str, str] = {
    "training_up": 'up{job="training"}',
    "training_current_epoch": "training_current_epoch",
    "training_current_loss": "training_current_loss",
    "checkpoint_save_timestamp": "training_checkpoint_save_timestamp_seconds",
    "checkpoint_save_duration": "training_checkpoint_save_duration_seconds",
    "checkpoint_file_size": "training_checkpoint_file_size_bytes",
    "training_resume_total": "training_resume_total",
}


class PrometheusQueryError(RuntimeError):
    """Prometheus 请求失败或响应格式不符合预期。"""


@dataclass(frozen=True)
class QueryResult:
    """保存一次查询的值和失败原因，便于计算 telemetry 完整性。"""

    value: float | None
    error: str | None = None


class PrometheusClient:
    """极小的 Prometheus HTTP API 客户端。

    使用标准库实现，避免为了一个研究 adapter 引入额外依赖。opener 参数
    允许单元测试注入假的网络函数，所以测试不需要启动 Prometheus。
    """

    def __init__(
        self,
        query_url: str = "http://127.0.0.1:9090/api/v1/query",
        timeout_seconds: float = 3.0,
        opener: Callable[..., object] | None = None,
    ) -> None:
        self.query_url = query_url
        self.timeout_seconds = timeout_seconds
        self._opener = opener or urlopen

    def query_scalar(self, promql: str) -> float | None:
        """执行 instant query，取结果向量中的第一个样本。"""

        request_url = f"{self.query_url}?{urlencode({'query': promql})}"
        request = Request(request_url, headers={"Accept": "application/json"})
        response = None
        try:
            # 生产环境下 urlopen 返回的对象支持上下文管理器；测试替身也
            # 可以只实现 read()，因此这里同时兼容两种形式。
            response = self._opener(request, timeout=self.timeout_seconds)
            payload = json.loads(response.read().decode("utf-8"))
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise PrometheusQueryError(str(error)) from error
        finally:
            # 真实 HTTPResponse 需要及时释放连接；测试替身没有 close 方法
            # 时也能正常工作。
            if response is not None and hasattr(response, "close"):
                response.close()

        if payload.get("status") != "success":
            raise PrometheusQueryError(payload.get("error", "Prometheus query failed"))

        results = payload.get("data", {}).get("result", [])
        if not results:
            return None
        value = results[0].get("value")
        if not isinstance(value, list) or len(value) < 2:
            raise PrometheusQueryError("Prometheus result has no scalar value")
        return float(value[1])


class PrometheusAdapter:
    """把当前训练指标转换为主动观测策略的输入快照。"""

    def __init__(self, client: PrometheusClient | None = None) -> None:
        self.client = client or PrometheusClient()

    #查询分析数据，查询错误原因
    def collect_metrics(self) -> tuple[dict[str, float], dict[str, str]]:
        """读取全部已知指标，返回成功值和失败原因。"""

        metrics: dict[str, float] = {}
        errors: dict[str, str] = {}
        for name, promql in METRIC_QUERIES.items():
            try:
                value = self.client.query_scalar(promql)
            except PrometheusQueryError as error:
                errors[name] = str(error)
                continue
            if value is None:
                errors[name] = "no sample returned"
                continue
            metrics[name] = value
        return metrics, errors

    #生产snapshot 所需要检测的数据
    def snapshot(
        self,
        budget: float,
        action_candidates: list[str] | None = None,
        timestamp: float | None = None,
    ) -> dict:
        """生成一条可直接交给 replay.py 的 JSON 对象。"""

        now = time.time() if timestamp is None else timestamp
        metrics, errors = self.collect_metrics()

        # checkpoint 时间戳来自训练进程成功完成原子替换之后的写入。
        # 通过它可以识别“进程仍然活着，但 checkpoint 已经停滞”。
        checkpoint_timestamp = metrics.get("checkpoint_save_timestamp")
        checkpoint_age = None
        if checkpoint_timestamp is not None:
            checkpoint_age = max(0.0, now - checkpoint_timestamp)

        # 这里把训练 exporter 的可用性和样本完整性映射成统一 telemetry
        # 字段。sample_loss 是缺失字段比例，范围约为 0 到 1。
        expected_count = len(METRIC_QUERIES)
        sample_loss = len(errors) / expected_count
        scrape_freshness = 1.0
        if "training_up" not in metrics or metrics.get("training_up") != 1.0:
            scrape_freshness = 0.0
        elif checkpoint_age is not None:
            # 30 秒是当前项目 TrainingCheckpointStale 告警使用的窗口。
            scrape_freshness = max(0.0, 1.0 - checkpoint_age / 30.0)

        telemetry = {
            "scrape_freshness": scrape_freshness,
            "sample_loss": sample_loss,
            "time_series_gaps": float("checkpoint_save_timestamp" in errors),
            "sensor_conflicts": 0.0,
        }
        normalized_metrics = dict(metrics)
        normalized_metrics["checkpoint_age_seconds"] = checkpoint_age if checkpoint_age is not None else -1.0
        normalized_metrics["training_up"] = metrics.get("training_up", 0.0)

        #要输出的结果
        snapshot = {
            "timestamp": now,
            "source": "prometheus",
            "metrics": normalized_metrics,
            "telemetry": telemetry,
            "budget": budget,
            "action_candidates": action_candidates or [],
            # 保留原始缺失原因，后续诊断器可以区分“没有样本”和“请求失败”。
            "adapter_errors": errors,
        }
        return snapshot


def _parse_actions(raw: str) -> list[str]:
    """解析命令行里的逗号分隔动作列表。"""

    return [item.strip() for item in raw.split(",") if item.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prometheus-url",
        default="http://127.0.0.1:9090/api/v1/query",
        help="Prometheus instant query API 地址",
    )
    parser.add_argument("--budget", type=float, default=8.0, help="本条快照允许的观测预算")
    parser.add_argument(
        "--actions",
        default="batch_adjustment,reroute",
        help="候选动作，使用逗号分隔",
    )
    parser.add_argument("--output", type=Path, help="输出 JSONL 文件；省略时输出到标准输出")
    parser.add_argument("--interval", type=float, default=0.0, help="连续采集间隔；0 表示只采集一次")
    parser.add_argument("--count", type=int, default=1, help="采集次数")
    args = parser.parse_args(argv)

    if args.budget < 0 or args.interval < 0 or args.count < 1:
        parser.error("budget、interval 不能为负数，count 必须大于 0")

    adapter = PrometheusAdapter(PrometheusClient(args.prometheus_url))
    output = args.output.open("w", encoding="utf-8") if args.output else sys.stdout
    try:
        for index in range(args.count):
            snapshot = adapter.snapshot(args.budget, _parse_actions(args.actions))
            output.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
            output.flush()
            if args.interval and index + 1 < args.count:
                time.sleep(args.interval)
    finally:
        if args.output:
            output.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
