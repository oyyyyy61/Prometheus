#K8s 恢复控制器：监听训练 Pod 的状态变化，发现故障后推动恢复并验证结果。
#
#这是 Kubernetes 所有内置控制器（Deployment、StatefulSet、Job……）共用的工作模式：
#  1. LIST  全量查询一次当前状态，建立"已知世界"的基线
#  2. WATCH 从基线的 resourceVersion 开始，流式接收后续每一个变化事件
#  3. 对比期望状态与实际状态，执行动作（这一步叫 reconcile）
#  4. WATCH 断线就回到第 1 步重新建立基线
#
#与旧版宿主机控制器（controller.py）的对比：
#  旧链路：Prometheus 抓取(5s) → 告警持续(15s) → Alertmanager 分组等待(5s)
#          → webhook → 恢复动作，端到端至少 25 秒
#  新链路：watch 直连 API server，Pod 状态变化秒级到达
#
#另一个重要区别是分工：
#  旧控制器自己 subprocess.Popen 拉起训练进程（既做决策又做执行）；
#  K8s 里"维持副本数"是 Deployment 控制器的职责，我们只负责
#  发现故障 → 清掉卡死的 Pod → 验证新 Pod 恢复了训练进度。
#  自己重新创建 Pod 等于和 Deployment 抢活干，是两个控制器打架的经典错误。

import json
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException
from prometheus_client import Counter, Gauge, start_http_server

#监听范围：training-demo 命名空间里带 app=training-demo 标签的 Pod
NAMESPACE = "training-demo"
LABEL_SELECTOR = "app=training-demo"

#宿主机可以直达集群内 Prometheus 的 ClusterIP（单节点集群的便利）。
#如果控制器以后搬进集群内部运行，应改为 Service DNS 名：
#http://monitoring-kube-prometheus-prometheus.monitoring:9090
PROMETHEUS_QUERY_URL = "http://10.233.61.150:9090/api/v1/query"

#WATCH 流的空闲超时。到期后流会正常结束，我们回到 LIST 重建基线。
#这能防止"连接看似还在、实际已经收不到事件"的僵死状态
WATCH_TIMEOUT_SECONDS = 60

#断线重连的退避时间起点（指数退避，封顶 30 秒）
RECONNECT_BACKOFF_SECONDS = 1

#恢复状态：False 表示当前有健康的训练 Pod；True 表示正在等待替补 Pod 恢复
recovery_in_progress = False
#记录故障发生的时间，用于计算端到端恢复耗时
recovery_started_at = 0.0
#记录已经处置过的 Pod 名字，防止对同一个卡死 Pod 反复执行删除
handled_pods = set()

#指标与旧控制器保持同名，方便在 Grafana 里对比两套方案；
#端口换成 9100，避免与旧的宿主机控制器（9001）同时运行时冲突
recovery_starts_total = Counter(
    "recovery_starts_total",
    "Number of recovery actions started by the k8s controller",
)
recovery_success_total = Counter(
    "recovery_success_total",
    "Number of recoveries verified by a healthy replacement pod",
)
training_pod_healthy = Gauge(
    "training_pod_healthy",
    "1 if a healthy training pod exists, 0 otherwise",
)


def log(message):
    #带上时间戳，故障注入演示时可以直接看出各环节耗时
    print(time.strftime("[%H:%M:%S]"), message, flush=True)


#判断一个 Pod 是否处于健康运行状态：
#phase 为 Running、所有容器 Ready、且没有被标记删除
def pod_is_healthy(pod):
    if pod.metadata.deletion_timestamp is not None:
        return False
    if pod.status.phase != "Running":
        return False
    statuses = pod.status.container_statuses or []
    if not statuses:
        return False
    return all(status.ready for status in statuses)


#从 Pod 状态中提取"发生了什么故障"的简短描述，用于日志
def describe_pod_problem(pod):
    phase = pod.status.phase
    statuses = pod.status.container_statuses or []
    for status in statuses:
        waiting = status.state.waiting
        if waiting and waiting.reason:
            return f"容器 {status.name} 处于 {waiting.reason}"
        terminated = status.state.terminated
        if terminated:
            return (
                f"容器 {status.name} 已退出"
                f"（reason={terminated.reason}, exit_code={terminated.exit_code}）"
            )
    return f"Pod phase={phase}"


#查询 Prometheus，确认新 Pod 确实从 Checkpoint 续上了训练进度。
#这一步是"恢复质量"的验证：Pod 活着 ≠ 训练真的续上了
def query_training_epoch():
    query_string = urlencode({"query": "training_current_epoch"})
    request_url = f"{PROMETHEUS_QUERY_URL}?{query_string}"
    try:
        with urlopen(request_url, timeout=3) as response:
            payload = json.load(response)
    except (URLError, TimeoutError, json.JSONDecodeError, OSError) as error:
        log(f"查询 Prometheus 失败（不影响恢复流程）: {error}")
        return None
    results = payload.get("data", {}).get("result", [])
    if not results:
        return None
    return float(results[0]["value"][1])


#核心 reconcile 逻辑：根据当前观察到的所有训练 Pod 的状态，决定是否需要动作。
#注意这是"基于当前状态"（level-based）的判断，而不是"基于单个事件"（edge-based）——
#事件可能丢失、重复、乱序，但每次事件到达后重新评估全局状态总是安全的，
#这是控制器设计的重要原则。
def reconcile(pods, api, event_hint):
    global recovery_in_progress, recovery_started_at

    healthy_pods = [p for p in pods if pod_is_healthy(p)]

    if healthy_pods:
        training_pod_healthy.set(1)
        if recovery_in_progress:
            #此前发生了故障，现在出现了健康的替补 Pod，恢复完成
            recovery_in_progress = False
            recovery_success_total.inc()
            elapsed = time.time() - recovery_started_at
            log(
                f"恢复成功：新 Pod {healthy_pods[0].metadata.name} 已就绪，"
                f"端到端耗时 {elapsed:.1f} 秒"
            )
            epoch = query_training_epoch()
            if epoch is not None:
                log(f"Prometheus 确认训练已续上：training_current_epoch = {epoch:.0f}")
        return

    #走到这里说明当前没有健康的训练 Pod
    training_pod_healthy.set(0)

    if recovery_in_progress:
        #已经在恢复流程中（比如已删除故障 Pod，正在等 Deployment 补新的），
        #静候下一个事件即可
        return

    recovery_in_progress = True
    recovery_started_at = time.time()
    recovery_starts_total.inc()
    log(f"检测到训练中断（{event_hint}），开始恢复流程")

    for pod in pods:
        name = pod.metadata.name
        if name in handled_pods:
            continue

        statuses = pod.status.container_statuses or []
        finished_ok = any(
            s.state.terminated and s.state.terminated.exit_code == 0 for s in statuses
        )
        if pod.status.phase == "Succeeded" or finished_ok:
            #exit code 0 表示训练脚本自己跑完了（epoch 达到上限），
            #这不是故障，不应该"恢复"它，否则会把已完成的任务反复拉起来
            log(f"Pod {name} 正常结束（训练完成），不执行恢复")
            handled_pods.add(name)
            continue

        problem = describe_pod_problem(pod)
        if pod.status.phase in ("Failed", "Unknown") or "BackOff" in problem or "Error" in problem:
            #Pod 已经卡死：Failed 不会被 Deployment 替换（它仍占用副本计数），
            #CrashLoopBackOff 是 kubelet 原地重启同一个容器，错误状态会一直持续。
            #这两种情况都需要我们删掉旧 Pod，Deployment 才会创建全新的 Pod
            log(f"删除卡死的 Pod {name}（{problem}），由 Deployment 自动补齐新 Pod")
            try:
                api.delete_namespaced_pod(name, NAMESPACE)
            except ApiException as error:
                log(f"删除 Pod {name} 失败: {error.status} {error.reason}")
            handled_pods.add(name)


#LIST + WATCH 主循环
def run():
    global RECONNECT_BACKOFF_SECONDS

    #从 ~/.kube/config 加载集群连接配置（宿主机运行方式）。
    #如果将来把控制器做成 Deployment 搬进集群，这里换成
    #config.load_incluster_config()，并配置 ServiceAccount + RBAC
    config.load_kube_config()
    api = client.CoreV1Api()
    log(f"K8s 恢复控制器已启动，监听 {NAMESPACE} 中标签为 {LABEL_SELECTOR} 的 Pod")

    while True:
        try:
            #第 1 步：LIST 全量查询，拿到当前状态和 resourceVersion（版本号）。
            #resourceVersion 是 etcd 的全局版本，WATCH 从这里开始就不会漏事件
            pod_list = api.list_namespaced_pod(NAMESPACE, label_selector=LABEL_SELECTOR)
            resource_version = pod_list.metadata.resource_version
            reconcile(list(pod_list.items), api, "控制器启动时的基线检查")

            #第 2 步：WATCH 从基线版本开始流式接收事件
            w = watch.Watch()
            for event in w.stream(
                api.list_namespaced_pod,
                NAMESPACE,
                label_selector=LABEL_SELECTOR,
                resource_version=resource_version,
                timeout_seconds=WATCH_TIMEOUT_SECONDS,
            ):
                event_type = event["type"]  #ADDED / MODIFIED / DELETED / ERROR
                if event_type == "ERROR":
                    #服务端主动报错（常见是 410 Gone：resourceVersion 太旧已被压缩），
                    #跳出内层循环，重新 LIST 建立新基线
                    log("WATCH 收到 ERROR 事件，重新 LIST")
                    break

                pod = event["object"]
                name = pod.metadata.name

                #DELETED 事件时 Pod 对象已不存在，需要单独提示
                if event_type == "DELETED":
                    log(f"事件：Pod {name} 已被删除")

                #每次事件到达后，用"API 当前真实状态"做 reconcile，
                #而不是只凭这一个事件做判断
                current_pods = api.list_namespaced_pod(
                    NAMESPACE, label_selector=LABEL_SELECTOR
                )
                reconcile(list(current_pods.items), api, f"事件 {event_type}: {name}")

            #WATCH 正常结束（超时），回到 LIST 重建基线
            RECONNECT_BACKOFF_SECONDS = 1

        except ApiException as error:
            if error.status == 410:
                #resourceVersion 太旧，etcd 已经丢弃了那段历史，必须重新 LIST
                log("resourceVersion 过期（410 Gone），重新 LIST")
                continue
            log(f"API 错误: {error.status} {error.reason}，{RECONNECT_BACKOFF_SECONDS} 秒后重连")
            time.sleep(RECONNECT_BACKOFF_SECONDS)
            RECONNECT_BACKOFF_SECONDS = min(RECONNECT_BACKOFF_SECONDS * 2, 30)
        except Exception as error:
            #网络抖动、连接被重置等都走这里：退避后重新 LIST + WATCH
            log(f"WATCH 连接中断: {error}，{RECONNECT_BACKOFF_SECONDS} 秒后重连")
            time.sleep(RECONNECT_BACKOFF_SECONDS)
            RECONNECT_BACKOFF_SECONDS = min(RECONNECT_BACKOFF_SECONDS * 2, 30)


if __name__ == "__main__":
    start_http_server(9100)
    print("控制器指标地址: http://127.0.0.1:9100/metrics", flush=True)
    run()
