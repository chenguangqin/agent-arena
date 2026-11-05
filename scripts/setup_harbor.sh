#!/usr/bin/env bash
# 一键准备 Harbor 评测环境，并跑通 demo task。
#
#   bash scripts/setup_harbor.sh
#
# 步骤：检查主机 → Docker 引擎（macOS 用 Colima，Linux 用系统 Docker）→ Compose/Buildx
# → uv → Harbor → 官方 hello-world（oracle）→ 下载 terminal-bench-sample 并跑一道（oracle）。
# 可重复执行：已完成的步骤会跳过。
#
# 可用环境变量覆盖：
#   HARBOR_VERSION   默认 0.24.0（与 devopsagent 已验证版本一致）
#   HARBOR_HOME      工作目录，默认 ~/.agent-arena/harbor（任务、jobs、env.sh 都放这里）
#   COLIMA_CPU / COLIMA_MEMORY / COLIMA_DISK   macOS 虚拟机资源，默认 4 / 8 / 60（GiB）
#   SKIP_TB_SAMPLE=1 只跑 hello-world，不下载 terminal-bench-sample
#   TB_SAMPLE_TASK   要跑的样例任务，默认 log-summary-date-ranges
set -euo pipefail

HARBOR_VERSION="${HARBOR_VERSION:-0.24.0}"
HARBOR_HOME="${HARBOR_HOME:-$HOME/.agent-arena/harbor}"
COMPOSE_VERSION="${COMPOSE_VERSION:-v5.6.0}"
BUILDX_VERSION="${BUILDX_VERSION:-v0.36.0}"
COLIMA_CPU="${COLIMA_CPU:-4}"
COLIMA_MEMORY="${COLIMA_MEMORY:-8}"
COLIMA_DISK="${COLIMA_DISK:-60}"
TB_SAMPLE_DATASET="${TB_SAMPLE_DATASET:-terminal-bench-sample@2.0}"
TB_SAMPLE_TASK="${TB_SAMPLE_TASK:-log-summary-date-ranges}"
SKIP_TB_SAMPLE="${SKIP_TB_SAMPLE:-0}"
TEMPLATE_REPO="https://github.com/harbor-framework/benchmark-template.git"

ENV_FILE="$HARBOR_HOME/env.sh"
STEP=0

step() { STEP=$((STEP + 1)); printf '\n\033[1;34m[%d] %s\033[0m\n' "$STEP" "$*"; }
info() { printf '    %s\n' "$*"; }
ok() { printf '    \033[32m✔ %s\033[0m\n' "$*"; }
die() { printf '\n\033[31m✘ %s\033[0m\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# 版本比较：ver_ge 0.36.0 0.17.0 → 真
ver_ge() { [ "$(printf '%s\n%s\n' "${1#v}" "${2#v}" | sort -V | head -1)" = "${2#v}" ]; }

OS="$(uname -s)"
ARCH="$(uname -m)"
mkdir -p "$HARBOR_HOME"
: >"$ENV_FILE.tmp"

# ---------------------------------------------------------------------------
step "检查主机"
info "系统 $OS / $ARCH，工作目录 $HARBOR_HOME"
for c in git curl; do have "$c" || die "缺少 $c"; done
if [ "$OS" = Darwin ]; then
  cpu=$(sysctl -n hw.ncpu); mem=$(($(sysctl -n hw.memsize) / 1024 / 1024 / 1024))
else
  cpu=$(nproc); mem=$(awk '/MemTotal/ {print int($2/1024/1024)}' /proc/meminfo)
fi
disk=$(df -Pk "$HARBOR_HOME" | awk 'NR==2 {print int($4/1024/1024)}')
info "CPU ${cpu} 核，内存 ${mem} GiB，可用磁盘 ${disk} GiB"
[ "$disk" -ge 30 ] || die "可用磁盘不足 30 GiB"
[ "$mem" -ge 8 ] || info "警告：内存低于 8 GiB，构建型任务可能失败"
ok "主机检查通过"

# ---------------------------------------------------------------------------
setup_docker_macos() {
  have brew || die "macOS 需要 Homebrew：https://brew.sh"
  local pkgs=()
  have colima || pkgs+=(colima)
  have docker || pkgs+=(docker)
  if [ ${#pkgs[@]} -gt 0 ]; then
    info "brew install ${pkgs[*]}"
    brew install "${pkgs[@]}"
  fi

  # 不依赖 Docker Desktop：独立的 docker 配置目录，不使用 desktop 凭证助手，
  # 只借用已有的 compose/buildx 插件。
  local cfg="$HARBOR_HOME/docker-config"
  mkdir -p "$cfg"
  local dirs=()
  for d in "$HOME/.docker/cli-plugins" "$(brew --prefix)/lib/docker/cli-plugins"; do
    [ -d "$d" ] && dirs+=("\"$d\"")
  done
  printf '{\n  "auths": {},\n  "cliPluginsExtraDirs": [%s]\n}\n' "$(IFS=,; echo "${dirs[*]-}")" >"$cfg/config.json"
  export DOCKER_CONFIG="$cfg"

  local vmargs=(--cpu "$COLIMA_CPU" --memory "$COLIMA_MEMORY" --disk "$COLIMA_DISK")
  if [ "$ARCH" = arm64 ]; then
    vmargs+=(--arch aarch64 --vm-type vz --vz-rosetta)   # 让 amd64 任务镜像通过 Rosetta 运行
  fi
  if colima status >/dev/null 2>&1; then
    ok "Colima 已在运行"
  else
    info "colima start ${vmargs[*]}（首次需下载虚拟机镜像，约几分钟）"
    colima start "${vmargs[@]}"
  fi
  local sock="$HOME/.colima/default/docker.sock"
  [ -S "$sock" ] || die "找不到 Colima 的 docker socket：$sock"
  export DOCKER_HOST="unix://$sock"

  # compose / buildx 插件不存在或过旧时用 brew 安装
  if ! docker compose version >/dev/null 2>&1; then
    brew install docker-compose
  fi
  local bx
  bx=$(docker buildx version 2>/dev/null | awk '{print $2}' || true)
  if [ -z "$bx" ] || ! ver_ge "$bx" 0.17.0; then
    brew install docker-buildx
  fi

  {
    echo "export DOCKER_CONFIG=\"$cfg\""
    echo "export DOCKER_HOST=\"$DOCKER_HOST\""
  } >>"$ENV_FILE.tmp"
}

install_cli_plugin() {  # name url
  local name=$1 url=$2 tmp
  tmp=$(mktemp)
  info "下载 $url"
  curl -fsSL "$url" -o "$tmp"
  sudo install -d -m 0755 /usr/local/lib/docker/cli-plugins
  sudo install -m 0755 "$tmp" "/usr/local/lib/docker/cli-plugins/$name"
  rm -f "$tmp"
}

setup_docker_linux() {
  if ! have docker; then
    if have dnf; then sudo dnf install -y docker
    elif have apt-get; then sudo apt-get update -y && sudo apt-get install -y docker.io
    else die "无法自动安装 Docker，请先手动安装"
    fi
  fi
  if have systemctl && ! systemctl is-active --quiet docker; then
    sudo systemctl enable --now docker
  fi
  if ! docker info >/dev/null 2>&1; then
    if sudo docker info >/dev/null 2>&1; then
      sudo usermod -aG docker "$USER"
      die "已把 $USER 加入 docker 组。请重新登录 SSH 后再次运行本脚本"
    fi
    die "Docker daemon 不可用：$(docker info 2>&1 | tail -1)"
  fi

  local carch barch
  case "$ARCH" in
    x86_64) carch=x86_64; barch=amd64 ;;
    aarch64|arm64) carch=aarch64; barch=arm64 ;;
    *) die "不支持的架构 $ARCH" ;;
  esac
  local cv
  cv=$(docker compose version --short 2>/dev/null || true)
  if [ -z "$cv" ] || ! ver_ge "$cv" 2.0.0; then
    install_cli_plugin docker-compose \
      "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${carch}"
  fi
  local bx
  bx=$(docker buildx version 2>/dev/null | awk '{print $2}' || true)
  if [ -z "$bx" ] || ! ver_ge "$bx" 0.17.0; then
    install_cli_plugin docker-buildx \
      "https://github.com/docker/buildx/releases/download/${BUILDX_VERSION}/buildx-${BUILDX_VERSION}.linux-${barch}"
  fi
}

step "准备 Docker 引擎"
if [ "$OS" = Darwin ]; then setup_docker_macos; else setup_docker_linux; fi
docker info >/dev/null 2>&1 || die "docker info 失败：$(docker info 2>&1 | tail -3)"
info "Server $(docker version --format '{{.Server.Version}}')，$(docker compose version)，buildx $(docker buildx version | awk '{print $2}')"
ok "Docker 可用"

# ---------------------------------------------------------------------------
step "安装 uv"
if ! have uv; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
ok "$(uv --version)"

# ---------------------------------------------------------------------------
step "安装 Harbor $HARBOR_VERSION"
export PATH="$HOME/.local/bin:$PATH"
current=$(harbor --version 2>/dev/null | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
if [ "$current" != "$HARBOR_VERSION" ]; then
  uv tool install --force "harbor==$HARBOR_VERSION"
fi
ok "harbor $(harbor --version 2>&1 | tail -1)"
echo "export PATH=\"\$HOME/.local/bin:\$PATH\"" >>"$ENV_FILE.tmp"
mv "$ENV_FILE.tmp" "$ENV_FILE"

# ---------------------------------------------------------------------------
# 读取 harbor job 的平均 reward（取 jobs/ 下最新的 result.json）
latest_mean() {
  local res
  res=$(find "$HARBOR_HOME/jobs" -maxdepth 2 -name result.json -newer "$1" 2>/dev/null | sort | tail -1)
  [ -n "$res" ] || { echo "NA"; return; }
  # job 级 result.json：stats.evals.<agent__dataset>.{n_errors, metrics[0].mean, exception_stats}
  python3 - "$res" <<'PY'
import json, sys
evals = json.load(open(sys.argv[1]))["stats"]["evals"]
for e in evals.values():
    if e.get("n_errors"):
        print("ERR " + ",".join(e.get("exception_stats", {})), file=sys.stderr)
        print("NA"); break
    print(f"{e['metrics'][0]['mean']:.3f}"); break
else:
    print("NA")
PY
}

run_oracle() {  # 描述 harbor-run 参数...
  local label=$1; shift
  local mark
  mark=$(mktemp)
  info "harbor run $* -a oracle -e docker"
  (cd "$HARBOR_HOME" && harbor run "$@" -a oracle -e docker) || die "$label 运行失败，查看 $HARBOR_HOME/jobs 下最新目录"
  local mean
  mean=$(latest_mean "$mark")
  rm -f "$mark"
  [ "$mean" = "1.000" ] || die "$label 的 oracle 得分为 $mean（期望 1.000），查看 $HARBOR_HOME/jobs 下最新目录"
  ok "$label：oracle 得分 1.000"
}

step "Harbor 官方 hello-world（oracle）"
if [ ! -d "$HARBOR_HOME/benchmark-template" ]; then
  git clone --depth 1 "$TEMPLATE_REPO" "$HARBOR_HOME/benchmark-template"
fi
run_oracle hello-world -p "$HARBOR_HOME/benchmark-template/tasks/hello-world"

# ---------------------------------------------------------------------------
if [ "$SKIP_TB_SAMPLE" != 1 ]; then
  step "下载 $TB_SAMPLE_DATASET 并运行 $TB_SAMPLE_TASK（oracle）"
  if [ ! -d "$HARBOR_HOME/benchmarks/${TB_SAMPLE_DATASET%@*}" ]; then
    (cd "$HARBOR_HOME" && harbor dataset download "$TB_SAMPLE_DATASET" --output-dir benchmarks --export)
  fi
  n=$(find "$HARBOR_HOME/benchmarks" -name instruction.md | wc -l | tr -d ' ')
  ok "已下载 $n 个任务到 $HARBOR_HOME/benchmarks"
  run_oracle "$TB_SAMPLE_TASK" -d "$TB_SAMPLE_DATASET" -i "$TB_SAMPLE_TASK"
fi

# ---------------------------------------------------------------------------
step "完成"
info "环境变量已写入 $ENV_FILE。新开终端运行 harbor 前先执行："
info "  source $ENV_FILE"
info "任务与结果：$HARBOR_HOME/benchmarks、$HARBOR_HOME/jobs"
