import time

from prometheus_client import Gauge, start_http_server

#执行命令：curl.exe -s http://127.0.0.1:8000/metrics | Select-String "training_current_epoch"
#可以用于监控任务执行到第几轮
#输出结果为：training_current_epoch x.0  x表示epoch的值
current_epoch_metric = Gauge(
    "training_current_epoch",
    "Latest completed training epoch",
)

start_http_server(8000)

epoch = 0

while True:
    epoch = epoch + 1
    current_epoch_metric.set(epoch)

    print("current epoch =", epoch)

    time.sleep(2)