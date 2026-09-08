#加载模型训练，并将训练的内容传入checkpoint保存



import torch
from torch import nn

x = torch.tensor([[1.0], [2.0], [3.0], [4.0]])
y = 3 * x + 2

#输入随机种子
torch.manual_seed(7)

#nn 是 PyTorch神经网络模块，Linear 表示线性层。它执行的计算可以写成：输出 = 输入 × 权重 + 偏置
model = nn.Linear(in_features=1, out_features=1)

print("model =", model)
print("initial weight =", model.weight.item())
print("initial bias =", model.bias.item())

#把张量 x 交给模型计算，
predictions = model(x)
print("predictions before training =", predictions)

#计算损失方差
loss_function = nn.MSELoss()
#首次随机种子和预期结果的误差
loss = loss_function(predictions, y)
print("loss before training =", loss.item())

#optimizer：优化器，负责调整模型参数
#torch.optim.SGD：随机梯度下降优化器
#model.parameters()：把模型中的 weight 和 bias 交给优化器管理
#lr=0.01：学习率，每次调整参数时的步幅。学习率太大会导致参数来回震荡，太小会导致学习速度很慢。
optimizer = torch.optim.SGD(model.parameters(), lr=0.01)
#PyTorch的梯度默认会累加。每次反向传播前先清空旧梯度，避免本轮梯度与上一轮混在一起
optimizer.zero_grad()
#反向传播
loss.backward()

print("weight gradient =", model.weight.grad.item())
print("bias gradient =", model.bias.grad.item())

#更新梯度
optimizer.step()
print("updated weight =", model.weight.item())
print("updated bias =", model.bias.item())

#更新后的新结果
updated_predictions = model(x)
updated_loss = loss_function(updated_predictions, y)
print("loss after one update =", updated_loss.item())


for epoch in range(2, 101):
    optimizer.zero_grad()
    predictions = model(x)
    loss = loss_function(predictions, y)

    loss.backward()
    optimizer.step()

    if epoch % 10 == 0:
        print("epoch =", epoch, "loss =", loss.item())

print("final weight =", model.weight.item())
print("final bias =", model.bias.item())

#设置快照，恢复记录后从第101轮开始训练
checkpoint = {
    "next_epoch": 101,
    #取得model参数
    "model_state_dict": model.state_dict(),
    #取得优化器参数
    "optimizer_state_dict": optimizer.state_dict(),
    #保存最近一次训练的 loss，方便恢复时查看之前训练到了什么水平
    "loss": loss.item(),
}
#将快照内容保存到practice_checkpoint.pt文件中
torch.save(checkpoint, "practice_checkpoint.pt")
print("checkpoint saved")