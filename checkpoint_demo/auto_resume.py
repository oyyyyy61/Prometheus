#通过文件是否存在判断是否已经开始训练，恢复训练从哪次开始



import signal
import time
import os
import torch
from torch import nn
from pathlib import Path
from prometheus_client import Counter, Gauge, start_http_server

#这里使用环境变量 TRAINING_CHECKPOINT_PATH 来指定 Checkpoint 的路径
#如果没有设置该环境变量，则默认使用当前目录下的 auto_checkpoint.pt
checkpoint_path = Path(
    os.environ.get("TRAINING_CHECKPOINT_PATH", "auto_checkpoint.pt")
)
print("Checkpoint path =", checkpoint_path.resolve())

#节点和 Pod 标签：在 K8s 中由 Deployment 通过 Downward API 自动注入，
#在宿主机直接运行时为空字符串，不影响使用。
#作用：多节点/多 Pod 时可以在 Prometheus 里按标签区分指标来自哪个实例
node_name = os.environ.get("TRAINING_NODE_NAME", "")
pod_name = os.environ.get("TRAINING_POD_NAME", "")
label_values = {"node": node_name, "pod": pod_name}

#监控最近完成并保存的 epoch
current_epoch_metric = Gauge(
    "training_current_epoch",
    "Latest completed and checkpointed training epoch",
    ["node", "pod"],
).labels(**label_values)
#监控最近一次训练 loss
current_loss_metric = Gauge(
    "training_current_loss",
    "Latest training loss",
    ["node", "pod"],
).labels(**label_values)
#监控最近一次 Checkpoint 成功写入的 Unix 时间戳
#作用：发现"进程活着但快照不更新"的故障（磁盘满、存储卡死）。
#此时 up 仍然是 1，只有这个时间戳停止增长，
#告警表达式：time() - training_checkpoint_save_timestamp_seconds > 30
checkpoint_save_timestamp_metric = Gauge(
    "training_checkpoint_save_timestamp_seconds",
    "Unix timestamp of the latest successful checkpoint save",
    ["node", "pod"],
).labels(**label_values)
#监控单次 Checkpoint 保存耗时
#作用：保存耗时持续上涨是存储性能劣化的早期信号
checkpoint_save_duration_metric = Gauge(
    "training_checkpoint_save_duration_seconds",
    "Duration of the latest checkpoint save in seconds",
    ["node", "pod"],
).labels(**label_values)
#监控 Checkpoint 文件大小
#作用：文件大小突然变小通常意味着写入被截断，快照不可信
checkpoint_file_size_metric = Gauge(
    "training_checkpoint_file_size_bytes",
    "Size of the checkpoint file in bytes",
    ["node", "pod"],
).labels(**label_values)
#统计从 Checkpoint 恢复的次数
#作用：让恢复事件本身可观测。进程重启后计数归零是正常现象，
#Prometheus 的 increase()/rate() 函数可以正确处理这种重置
resume_total_metric = Counter(
    "training_resume_total",
    "Number of times training resumed from a checkpoint",
    ["node", "pod"],
).labels(**label_values)
#训练运行状态：1 表示正在执行训练，0 表示已停止或已完成。
training_active_metric = Gauge(
    "training_active",
    "Whether the training loop is actively processing epochs",
    ["node", "pod"],
).labels(**label_values)
#训练是否已经达到目标 epoch。
training_completed_metric = Gauge(
    "training_completed",
    "Whether training has reached the configured target epoch",
    ["node", "pod"],
).labels(**label_values)
#训练目标和批大小用于解释进度与吞吐量。
training_target_epoch_metric = Gauge(
    "training_target_epoch",
    "Configured target training epoch",
    ["node", "pod"],
).labels(**label_values)
training_batch_size_metric = Gauge(
    "training_batch_size",
    "Number of samples processed in the latest batch",
    ["node", "pod"],
).labels(**label_values)
#Counter 适合用 rate() 计算长期训练吞吐。
training_samples_total_metric = Counter(
    "training_samples_total",
    "Total number of samples processed by the training loop",
    ["node", "pod"],
).labels(**label_values)
training_samples_per_second_metric = Gauge(
    "training_samples_per_second",
    "Samples processed per second during the latest training step",
    ["node", "pod"],
).labels(**label_values)
training_data_loading_wait_metric = Gauge(
    "training_data_loading_wait_seconds",
    "Time spent waiting for the latest training batch",
    ["node", "pod"],
).labels(**label_values)
training_step_duration_metric = Gauge(
    "training_step_duration_seconds",
    "Duration of the latest forward/backward/optimizer step",
    ["node", "pod"],
).labels(**label_values)
#Checkpoint 成功存在时为 1；启动时没有可用快照则为 0。
checkpoint_status_metric = Gauge(
    "training_checkpoint_status",
    "Whether a valid checkpoint is currently available",
    ["node", "pod"],
).labels(**label_values)

start_http_server(8000)


torch.manual_seed(7)

x = torch.tensor([[1.0], [2.0], [3.0], [4.0]])
y = 3 * x + 2

model = nn.Linear(1, 1)
optimizer = torch.optim.SGD(model.parameters(), lr=0.01)
loss_function = nn.MSELoss()

start_epoch = 1
total_epochs = 3000
training_target_epoch_metric.set(total_epochs)
training_active_metric.set(1)
training_completed_metric.set(0)
checkpoint_status_metric.set(0)

if checkpoint_path.exists():
    print("发现 Checkpoint，准备恢复训练")
    checkpoint = torch.load(
        checkpoint_path,
        map_location="cpu",
        weights_only=True,
    )

    model.load_state_dict(checkpoint["model_state_dict"])
    optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
    start_epoch = int(checkpoint["next_epoch"])
    resume_total_metric.inc()

    #恢复后立即把指标对齐到 Checkpoint 记录的进度。
    #否则从重启到第一个 epoch 完成之间，Prometheus 看到的进度会回落到 0，
    #监控曲线上会出现一次假的"进度归零"
    current_epoch_metric.set(start_epoch - 1)
    current_loss_metric.set(float(checkpoint["loss"]))

    #快照的时间戳和大小反映的是磁盘上文件的真实状态（mtime），
    #这样即使进程刚恢复还没完成第一轮保存，"快照停滞"告警的时间基准也是准确的
    checkpoint_save_timestamp_metric.set(checkpoint_path.stat().st_mtime)
    checkpoint_file_size_metric.set(checkpoint_path.stat().st_size)
    checkpoint_status_metric.set(1)

    print("Checkpoint 加载完成")
else:
    print("没有 Checkpoint，准备首次训练")

print("start epoch =", start_epoch)
print("current weight =", model.weight.item())
print("current bias =", model.bias.item())

temporary_checkpoint_path = checkpoint_path.with_suffix(".pt.tmp")

#创建停止标志。False 表示继续训练
stop_requested = False
#定义信号处理函数
def request_stop(signal_number, current_frame):
    #说明函数要修改外部的 stop_requested 变量
    global stop_requested
    stop_requested = True
    print("\n收到退出信号，将在安全位置停止训练")
#分别监听 Ctrl+C 常用的SIGINT和 Kubernetes常用的 SIGTERM
signal.signal(signal.SIGINT, request_stop)
signal.signal(signal.SIGTERM, request_stop)

try:
    for epoch in range(start_epoch, total_epochs + 1):
        #当前示例数据已经在内存中；保留这段计时边界，替换成 DataLoader 后
        #会自然反映真实的数据读取、预处理或队列等待时间。
        batch_load_start = time.perf_counter()
        batch_x, batch_y = x, y
        data_loading_wait = time.perf_counter() - batch_load_start
        training_data_loading_wait_metric.set(data_loading_wait)
        training_batch_size_metric.set(batch_x.shape[0])

        step_start = time.perf_counter()
        optimizer.zero_grad()

        predictions = model(batch_x)
        loss = loss_function(predictions, batch_y)

        loss.backward()
        optimizer.step()
        step_duration = time.perf_counter() - step_start
        training_step_duration_metric.set(step_duration)
        training_samples_total_metric.inc(batch_x.shape[0])
        training_samples_per_second_metric.set(batch_x.shape[0] / max(step_duration, 1e-9))

        checkpoint_to_save = {
            "next_epoch": epoch + 1,
            "model_state_dict": model.state_dict(),
            "optimizer_state_dict": optimizer.state_dict(),
            "loss": loss.item(),
        }

        #统计快照保存耗时，计时覆盖"写临时文件 + 原子替换"的完整落盘过程
        save_start = time.perf_counter()
        torch.save(checkpoint_to_save, temporary_checkpoint_path)
        os.replace(temporary_checkpoint_path, checkpoint_path)
        save_duration = time.perf_counter() - save_start

        #使Prometheus看到的 epoch 已经拥有可用于恢复的 Checkpoint
        current_epoch_metric.set(epoch)
        current_loss_metric.set(loss.item())

        #以下三个指标描述"快照本身"的健康度，必须在 os.replace 成功后更新，
        #保证 Prometheus 看到的时间戳一定对应一个完整可用的文件
        checkpoint_save_timestamp_metric.set(time.time())
        checkpoint_save_duration_metric.set(save_duration)
        checkpoint_file_size_metric.set(checkpoint_path.stat().st_size)
        checkpoint_status_metric.set(1)

        if stop_requested:
            print("当前轮 Checkpoint 已保存，训练安全停止")
            break

        print(
            "completed epoch =", epoch,
            "next epoch =", epoch + 1,
            "loss =", loss.item(),
            "checkpoint saved",
        )

        time.sleep(1)

except KeyboardInterrupt:
    print("\n训练被手动暂停")
    print("下次启动将读取最近一次成功保存的 Checkpoint")
finally:
    training_active_metric.set(0)

# Deployment 的 restartPolicy 固定为 Always。
# 训练完成后保持进程存活，避免 kubelet 反复重启一个已完成的任务。
if checkpoint_path.exists() and not stop_requested:
    final_checkpoint = torch.load(
        checkpoint_path,
        map_location="cpu",
        weights_only=True,
    )
    if int(final_checkpoint["next_epoch"]) > total_epochs:
        training_completed_metric.set(1)
        print("训练已完成，保持 Pod 运行以维持 Deployment 副本", flush=True)
        while not stop_requested:
            time.sleep(30)
