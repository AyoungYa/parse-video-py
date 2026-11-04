# 算力机镜像（Linux + NVIDIA GPU 场景）。
#
# ⚠️ Windows 上更推荐「原生安装」而不是 Docker：
#    原生装 CUDA + torch 最快，Docker 还要额外的 WSL2 + nvidia-container-toolkit。
#    原生安装步骤见 README。
#
# 构建前确认宿主机已有驱动，且装了 nvidia-container-toolkit。

FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    DJANGO_SETTINGS_MODULE=config.settings

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip python3-venv ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

# torch 单独装（CUDA 12.4 对应 cu124 源）。要换版本就改这一行。
RUN pip3 install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cu124

COPY . .

# 工作区挂载进来即可，镜像里不存素材
VOLUME ["/srv/workspace", "/srv/scripts"]

CMD ["python3", "manage.py", "run_worker"]
