#!/usr/bin/env bash
set -euo pipefail

# CentOS 7 上“一键更新代码并部署到 Docker”的入口脚本。
# 用法：
#   export REPO_URL="https://github.com/<owner>/<repo>.git"
#   export APP_DIR="/opt/api2cursor"
#   export BRANCH="main"
#   bash deploy-centos7.sh

APP_DIR="${APP_DIR:-/www/wwwroot/api2cursor}"
REPO_URL="${REPO_URL:-}"
BRANCH="${BRANCH:-}"

log() {
  echo "[$(date '+%F %T')] $*"
}

cleanup() {
  # 这里只预留清理逻辑，确保 Ctrl+C / 终止时能安全退出。
  true
}
trap cleanup INT TERM

if [[ -z "${REPO_URL}" ]]; then
  log "ERROR: REPO_URL 为空。请设置 REPO_URL 后再运行。"
  exit 1
fi

docker_compose() {
  # 优先使用 docker-compose（老版本），否则使用 docker compose（新版本）。
  if command -v docker-compose >/dev/null 2>&1; then
    docker-compose "$@"
  else
    docker compose "$@"
  fi
}

ensure_app_dir() {
  if [[ ! -d "${APP_DIR}" ]]; then
    log "目录不存在，开始 clone: ${APP_DIR}"
    mkdir -p "${APP_DIR}"
    if [[ -n "${BRANCH}" ]]; then
      git clone --branch "${BRANCH}" --single-branch "${REPO_URL}" "${APP_DIR}"
    else
      git clone "${REPO_URL}" "${APP_DIR}"
    fi
    return
  fi

  if [[ ! -d "${APP_DIR}/.git" ]]; then
    log "ERROR: ${APP_DIR} 已存在，但不是 git 仓库。"
    exit 1
  fi
}

detect_branch_if_needed() {
  if [[ -n "${BRANCH}" ]]; then
    return
  fi
  if [[ -z "${REPO_URL}" ]]; then
    BRANCH="main"
    log "未设置 REPO_URL 时无法自动识别分支，使用默认 BRANCH=main"
    return
  fi

  if git ls-remote --heads "${REPO_URL}" "main" | grep -q "refs/heads/main"; then
    BRANCH="main"
    return
  fi
  if git ls-remote --heads "${REPO_URL}" "master" | grep -q "refs/heads/master"; then
    BRANCH="master"
    return
  fi

  log "ERROR: 无法自动识别 main/master 分支，请显式设置 BRANCH。"
  exit 1
}

update_code() {
  cd "${APP_DIR}"

  detect_branch_if_needed

  log "拉取最新代码：${REPO_URL} (${BRANCH})"
  git fetch --prune origin "${BRANCH}"

  # 让工作区与远端保持一致（不会删除未跟踪的 data 等目录）。
  git checkout -B "${BRANCH}" "origin/${BRANCH}"
  git pull --rebase
}

ensure_env_and_data() {
  cd "${APP_DIR}"

  env_created=false
  if [[ ! -f ".env" ]]; then
    if [[ -f ".env.example" ]]; then
      # 仅在首次部署时复制模板；后续更新会保留现有 .env（避免覆盖密钥）。
      cp ".env.example" ".env"
      env_created=true
      log "已创建服务器上的 .env（请立刻编辑为真实密钥/地址）。"
    else
      log "ERROR: .env 不存在，且 .env.example 也不存在。请手动创建 .env。"
      exit 1
    fi
  fi

  mkdir -p data

  if [[ "${env_created}" == "true" ]]; then
    log "已生成占位 .env，当前不会继续部署。请编辑 .env 后重新运行脚本。"
    exit 0
  fi
}

deploy() {
  cd "${APP_DIR}"

  # compose.yml 中 service 名为 api2cursor，环境变量由 .env 提供。
  log "执行 docker compose：build + up"
  docker_compose up -d --build --remove-orphans

  log "部署完成，服务状态："
  docker_compose ps
  log "如需排障：docker_compose logs -n 200"
}

ensure_app_dir
update_code
ensure_env_and_data
deploy

