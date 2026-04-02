# CentOS 7 部署与更新（Docker 重部署，`/www/wwwroot/api2cursor`）

本文件记录如何把 `api2cursor` 部署到服务器目录 `/www/wwwroot/api2cursor`，以及后续如何安全更新代码并用 Docker 重建容器（保留服务器上的 `.env`）。

> 约定：仓库里 `.env` 已被 `.gitignore` 忽略，因此**不要把 `.env` 通过 git 上传/覆盖**。

> 你的仓库地址：`https://github.com/yfcyfc123234/api2cursor.git`

---

## 1. 前置条件（建议已安装好 Docker / Compose）

确保安装并启动 Docker：

```bash
sudo systemctl enable docker
sudo systemctl start docker
```

检查命令可用性（任选其一）：

```bash
docker compose version
# 或
docker-compose version
```

同时需要 `git`：

```bash
sudo yum -y install git
```

---

## 2. 创建部署目录

```bash
sudo mkdir -p /www/wwwroot
cd /www/wwwroot
```

---

## 3. 第一次部署（clone + docker compose up）

如果 `/www/wwwroot/api2cursor` 还不是一个 git 仓库（目录里没有 `.git`），执行 clone：

```bash
sudo rm -rf api2cursor
sudo git clone https://github.com/yfcyfc123234/api2cursor.git api2cursor
cd api2cursor
```

准备服务器上的 `.env`（只在服务器上操作，密钥/地址填你的真实值）：

```bash
cp .env.example .env
nano .env
```

使用 Docker 部署（会构建镜像并以 compose 方式启动）：

```bash
docker compose up -d --build
# 或旧版：docker-compose up -d --build
```

启动后访问（端口取决于 `.env` 的 `PROXY_PORT`，默认 `3029`）：

```text
http://127.0.0.1:3029/admin
```

---

## 4. 如何更新代码（git pull + Docker 重建）

更新核心原则：

- 只拉取代码（保留服务器上的 `.env` 和 `./data` 挂载目录）
- 更新后执行 `docker compose up -d --build` 重建并重启

在服务器上执行：

```bash
cd /www/wwwroot/api2cursor

git fetch --all --prune
git checkout main 2>/dev/null || git checkout master
git pull --rebase

docker compose up -d --build --remove-orphans
# 或旧版：docker-compose up -d --build --remove-orphans
```

---

## 5. 推荐：使用项目自带的一键部署脚本（可选）

仓库里我已经加了脚本：`scripts/deploy-centos7.sh`

你可以在服务器上直接执行（会进行拉取 + `docker compose up -d --build`，并且当服务器没有 `.env` 时只会用 `.env.example` 生成一次）：

```bash
cd /www/wwwroot/api2cursor

export REPO_URL="https://github.com/yfcyfc123234/api2cursor.git"
export APP_DIR="/www/wwwroot/api2cursor"
# BRANCH 可选：不填则自动识别 main/master
export BRANCH=""

bash scripts/deploy-centos7.sh
```

如果脚本提示“已生成占位 .env，当前不会继续部署”，说明这是首次部署。你需要编辑 `/www/wwwroot/api2cursor/.env` 后再运行一遍脚本即可完成部署。

---

## 6. `.env` 不要被覆盖的注意事项

- 服务器上的 `.env` 是你填的密钥与配置，已经被 `.gitignore` 忽略。
- 更新时只做 `git pull` / `git fetch` / `git checkout`，不要 `rm -rf` 重新 clone（除非你明确要重新生成 `.env`）。

---

## 7. 常见排查

1）容器没起来：

```bash
docker compose ps
```

2）查看日志：

```bash
docker compose logs -n 200 api2cursor
```

3）端口访问不通：检查防火墙/安全组是否放行 `PROXY_PORT`（默认 `3029`）。

