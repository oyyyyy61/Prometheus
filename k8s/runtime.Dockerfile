#训练运行时镜像：只包含 Python + PyTorch(CPU 版) + 指标库，不包含训练代码。
#训练脚本通过 ConfigMap 挂载进容器（见 deployment.yaml），
#这样修改脚本不需要重新构建镜像，镜像是通用的"训练运行时"。
FROM python:3.12-slim

#离线安装：wheel 包预先在宿主机下载好（见 k8s/README.md），
#构建过程完全不需要访问外网，避免跨境 CDN 传输停滞导致构建卡死。
#--no-index 禁止访问任何远程索引，--find-links 从本地目录解析依赖。
COPY wheels/ /tmp/wheels/
RUN pip install --no-cache-dir --no-index --find-links /tmp/wheels \
        torch prometheus_client \
    && rm -rf /tmp/wheels

#启动命令由 Deployment 的 command 字段指定（运行 ConfigMap 挂载的脚本），
#所以这里不需要 CMD/ENTRYPOINT
