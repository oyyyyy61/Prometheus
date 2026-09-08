#读取checkpoint的内容，沿中断前的内容继续进行训练



import torch
import time
import os
from torch import nn

checkpoint = torch.load(
    #从对应文件中读取checkpoint的内容
    "practice_checkpoint.pt",
    map_location="cpu",
    weights_only=True,
)

print("checkpoint keys =", checkpoint.keys())
print("next epoch =", checkpoint["next_epoch"])
print("saved loss =", checkpoint["loss"])
print("model state =", checkpoint["model_state_dict"])
print("optimizer state =", checkpoint["optimizer_state_dict"])

#生成新模型和优化器
new_model = nn.Linear(1, 1)
new_optimizer = torch.optim.SGD(new_model.parameters(), lr=0.01)

print("before loading weight =", new_model.weight.item())
print("before loading bias =", new_model.bias.item())

#将快照保存的数据注入新模型和优化器
new_model.load_state_dict(checkpoint["model_state_dict"])
new_optimizer.load_state_dict(checkpoint["optimizer_state_dict"])

print("after loading weight =", new_model.weight.item())
print("after loading bias =", new_model.bias.item())


#重新载入训练
x = torch.tensor([[1.0], [2.0], [3.0], [4.0]])
y = 3 * x + 2

loss_function = nn.MSELoss()

start_epoch = checkpoint["next_epoch"]
total_epochs = 1000

#临时文件替换,防止文件写入一半暂停服务
checkpoint_path = "practice_checkpoint.pt"
temporary_checkpoint_path = checkpoint_path + ".tmp"

for epoch in range(start_epoch, total_epochs + 1):
    new_optimizer.zero_grad()

    predictions = new_model(x)
    loss = loss_function(predictions, y)

    loss.backward()
    new_optimizer.step()

    checkpoint_to_save = {
        "next_epoch": epoch + 1,
        "model_state_dict": new_model.state_dict(),
        "optimizer_state_dict": new_optimizer.state_dict(),
        "loss": loss.item(),
    }

    torch.save(checkpoint_to_save, temporary_checkpoint_path)
    os.replace(temporary_checkpoint_path, checkpoint_path)

    print(
        "completed epoch =", epoch,
        "next epoch =", epoch + 1,
        "loss =", loss.item(),
        "checkpoint saved",
    )
#休眠一秒，有时间捕获节点
    time.sleep(1)