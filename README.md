# GPU Monitor

一个面向内网 GPU 集群的轻量监控面板，满足以下需求：

- 状态层：展示每台服务器每张 GPU 的当前利用率、显存、活跃用户、占用状态。
- 历史统计层：支持 7 / 14 / 20 / 30 天窗口，展示 GPU 占用率、有效利用率、平均 GPU utilization 与平均显存。
- 用户统计层：按用户聚合 GPU 使用时长、非空闲时长与平均利用率。
- 采样策略：默认每 10 分钟通过 SSH 执行 `nvidia-smi`，只保留每日聚合结果，自动清理 60 天前数据。

## 业务规则

- 服务器：`10.193.104.165 / 170 / 181 / 182 / 186`
- 页面部署地址：`PZU-104-165`
- 空闲定义：`utilization.gpu < 10%`
- 占用定义：存在 GPU 计算进程即视为占用
- 有效利用率：非空闲时间 / 总时间
- 用户过滤：`dataset_model`、`lost+found`、`tempuser` 不计入统计
- 页面权限：不做注册登录，用户输入自己的 SSH 信息后，只展示其可以访问的服务器数据

## 架构说明

### 1. 定时采集

- 后台调度器每 10 分钟执行一次采集。
- 采集器使用部署机上的统一采集账号（通过环境变量配置）SSH 到 5 台服务器。
- 采集命令：
  - `nvidia-smi --query-gpu=index,name,uuid,utilization.gpu,memory.used,memory.total,temperature.gpu`
  - `nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory`
  - `ps -eo pid=,user=`
- 程序会将 PID 对应的 Linux 用户聚合到 GPU 维度，再滚动累加为每日聚合数据。

### 2. 用户可见性控制

- 首页要求用户输入 Linux 用户名与密码，或勾选 “使用部署机上的 SSH key / agent”。
- 后端会对每台服务器执行一次 `echo ok` 校验可达性。
- 校验通过的服务器列表写入 session，后续 API 只返回这些服务器的数据。

### 3. 数据存储

- `current_gpu_statuses`：当前状态快照。
- `daily_gpu_aggregates`：按天聚合的 GPU 统计。
- `daily_user_aggregates`：按天聚合的用户统计。
- 默认数据库：SQLite，文件位置通常是 `./data/gpu_monitor.db`。

## 用 Conda 安装并运行

下面假设你在部署机 `10.193.104.165` / `PZU-104-165` 上操作，仓库目录为 `~/GPUMonitor`。

### 方式 A：直接用 `environment.yml` 创建环境

```bash
git clone <your-repo-url> ~/GPUMonitor
cd ~/GPUMonitor
conda env create -f environment.yml
conda activate gpu-monitor
cp .env.example .env
```

然后编辑 `.env`，至少填这些字段：

```bash
SECRET_KEY=replace-with-a-random-string
DATABASE_URL=sqlite:///./data/gpu_monitor.db
COLLECTOR_INTERVAL_MINUTES=10
RETENTION_DAYS=60
COLLECTOR_SSH_USERNAME=<部署机上用于采集的统一账号>
COLLECTOR_SSH_PASSWORD=<对应密码，或者留空改用 SSH key>
# 如果你要用密钥，就填写：
# COLLECTOR_SSH_KEY_PATH=/absolute/path/to/id_ed25519
```

启动应用：

```bash
mkdir -p data
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

如果你想开发时自动热更新，可以用：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

浏览器访问：

```text
http://127.0.0.1:8000
```

如果你是从其它机器访问部署机，可以打开：

```text
http://PZU-104-165:8000
```

### 方式 B：手动创建 Conda 环境

```bash
git clone <your-repo-url> ~/GPUMonitor
cd ~/GPUMonitor
conda create -n gpu-monitor python=3.12 -y
conda activate gpu-monitor
pip install -r requirements.txt
cp .env.example .env
mkdir -p data
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 首次运行后建议做的检查

### 1. 看服务是否启动成功

```bash
curl http://127.0.0.1:8000/
```

### 2. 手动触发一次采集

```bash
curl -X POST http://127.0.0.1:8000/api/collector/run
```

如果配置正确，返回里会看到类似：

```json
{"messages":["Collected 10.193.104.165","Collected 10.193.104.170"]}
```

### 3. 查看当前状态 API

```bash
curl http://127.0.0.1:8000/api/status/current
```

注意：这个接口依赖浏览器 session。最简单的方式是先在页面里输入你自己的 SSH 用户信息，再刷新页面看面板。

## 用 Docker Compose 运行

先配置环境变量，例如：

```bash
export COLLECTOR_SSH_USERNAME=your-collector-user
export COLLECTOR_SSH_PASSWORD=your-collector-password
# 或者 export COLLECTOR_SSH_KEY_PATH=/keys/id_ed25519
```

然后启动：

```bash
docker compose up -d --build
```

查看日志：

```bash
docker compose logs -f gpu-monitor
```

## 如何下载数据库

当前默认使用 SQLite，所以“下载数据库”本质上就是把 SQLite 文件从部署机拷走。

### 方案 1：直接下载数据库文件

如果应用的 `.env` 里是默认配置：

```bash
DATABASE_URL=sqlite:///./data/gpu_monitor.db
```

那么数据库文件通常在：

```bash
~/GPUMonitor/data/gpu_monitor.db
```

你可以先在部署机确认：

```bash
cd ~/GPUMonitor
ls -lh data/gpu_monitor.db
```

然后在你自己的电脑上执行：

```bash
scp <your-user>@10.193.104.165:~/GPUMonitor/data/gpu_monitor.db ./gpu_monitor.db
```

如果你是从 Windows PowerShell 下载，也可以这样写：

```powershell
scp <your-user>@10.193.104.165:~/GPUMonitor/data/gpu_monitor.db .\gpu_monitor.db
```

### 方案 2：先做一个备份副本，再下载

为了避免直接拷正在使用的数据库，仓库里带了一个备份脚本：`scripts/backup_sqlite.py`。

在部署机上执行：

```bash
cd ~/GPUMonitor
conda activate gpu-monitor
python scripts/backup_sqlite.py --database-url sqlite:///./data/gpu_monitor.db --output ./backups/gpu_monitor_backup.db
```

这条命令会输出备份文件路径。然后你再从本地下载：

```bash
scp <your-user>@10.193.104.165:~/GPUMonitor/backups/gpu_monitor_backup.db ./gpu_monitor_backup.db
```

### 方案 3：如果你已经用了 Docker Compose

数据库仍然是挂载到宿主机目录 `./data`，所以仍然可以直接从宿主机下载：

```bash
cd ~/GPUMonitor
ls -lh data/gpu_monitor.db
scp <your-user>@10.193.104.165:~/GPUMonitor/data/gpu_monitor.db ./gpu_monitor.db
```

## 常用运维命令

### 更新代码后重启

```bash
cd ~/GPUMonitor
git pull
conda activate gpu-monitor
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 如果是 Docker Compose 部署

```bash
cd ~/GPUMonitor
git pull
docker compose up -d --build
```

### 查看最近数据库文件大小

```bash
cd ~/GPUMonitor
ls -lh data/
```

## 后续建议

1. 在 `10.193.104.165` 上为采集器配置一个统一只读账号或专用 SSH key。
2. 将 SQLite 切换到 PostgreSQL，用于长期稳定运行。
3. 视需要增加告警、CSV 导出与用户/服务器筛选。
