# PyTorch Checkpoint 入门实验

这个实验训练一个一元线性模型：

```text
y = 3x + 2
```

脚本在每个 epoch 完成后保存一次 `checkpoint.pt`。关闭程序再重新启动时，脚本会自动读取该文件，并从记录的下一个 epoch 继续训练。

## 1. 从头开始训练

在 PowerShell 中执行：

```powershell
Set-Location E:\codex\Prometheus\checkpoint_demo
E:\env\envs\pytorch\python.exe train.py --fresh --epochs 20
```

看到几轮输出后按 `Ctrl+C`。例如：

```text
[epoch 005/020] loss=...; checkpoint saved
^C
[stopped] Interrupted. The most recently completed epoch is safe.
```

`--fresh` 只用于本实验，它会在启动时删除旧的 `checkpoint.pt`。

## 2. 从 Checkpoint 继续训练

再次执行，这次去掉 `--fresh`：

```powershell
E:\env\envs\pytorch\python.exe train.py --epochs 20
```

脚本会先显示：

```text
[resume] Loaded ...\checkpoint.pt. Next epoch: 5; previous loss: ...
```

随后从第 6 次训练开始，最终完成 20 个 epoch。

## 3. Checkpoint 中保存了什么

`train.py` 保存了：

- `model_state_dict`：模型参数。
- `optimizer_state_dict`：优化器状态。
- `next_epoch`：恢复后要执行的下一个 epoch。
- `loss`：最近一次保存时的损失值。
- `torch_rng_state`：PyTorch 随机数生成器状态。
- `schema_version`：Checkpoint 数据结构版本。

## 4. 为什么使用临时文件

脚本先写入 `checkpoint.pt.tmp`，完整写入后再替换 `checkpoint.pt`。这样可以降低程序恰好在保存过程中被关闭时损坏正式 Checkpoint 的风险。

## 5. 本实验的恢复边界

每个 epoch 结束时才会保存。程序在一个 epoch 中途退出时，这一轮尚未完成的计算会丢失，已完成并保存的 epoch 可以恢复。

真实训练还需要保存学习率调度器、混合精度 scaler、数据加载位置、分布式 Rank 信息，以及代码和配置版本。
