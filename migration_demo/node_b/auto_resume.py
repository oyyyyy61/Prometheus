#通过文件是否存在判断是否已经开始训练，恢复训练从哪次开始



import signal
import time
import os
import torch
from torch import nn
from pathlib import Path
from prometheus_client import Gauge, start_http_server


checkpoint_path = Path("auto_checkpoint.pt")

#监控最近完成并保存的 epoch
current_epoch_metric = Gauge(
    "training_current_epoch",
    "Latest completed and checkpointed training epoch",
)
#监控最近一次训练 loss
current_loss_metric = Gauge(
    "training_current_loss",
    "Latest training loss",
)

start_http_server(8000)


torch.manual_seed(7)

x = torch.tensor([[1.0], [2.0], [3.0], [4.0]])
y = 3 * x + 2

model = nn.Linear(1, 1)
optimizer = torch.optim.SGD(model.parameters(), lr=0.01)
loss_function = nn.MSELoss()

start_epoch = 1
total_epochs = 1000

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
        optimizer.zero_grad()

        predictions = model(x)
        loss = loss_function(predictions, y)

        loss.backward()
        optimizer.step()

        checkpoint_to_save = {
            "next_epoch": epoch + 1,
            "model_state_dict": model.state_dict(),
            "optimizer_state_dict": optimizer.state_dict(),
            "loss": loss.item(),
        }

        torch.save(checkpoint_to_save, temporary_checkpoint_path)
        os.replace(temporary_checkpoint_path, checkpoint_path)

        #使Prometheus看到的 epoch 已经拥有可用于恢复的 Checkpoint
        current_epoch_metric.set(epoch)
        current_loss_metric.set(loss.item())

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