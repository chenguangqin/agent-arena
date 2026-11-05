#!/usr/bin/env bash
# 生成 τ³-bench 的 Harbor 任务（默认只生成 banking_knowledge 域），并安装 arena 评测依赖。
# 前置：已运行 scripts/setup_harbor.sh。
#
#   bash scripts/prepare_tau3.sh
#
# 环境变量：
#   TAU3_DOMAINS   默认 banking_knowledge（可选 airline,retail,telecom,banking_knowledge）
#   TAU3_LIMIT     只生成前 N 个任务，默认不限
#   HARBOR_REF     Harbor 仓库版本（adapter 来源），默认 v0.24.0
#   TAU2_REF       tau2-bench 版本（任务数据与镜像内安装的版本），默认 v1.0.1
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HARBOR_HOME="${HARBOR_HOME:-$HOME/.agent-arena/harbor}"
HARBOR_REF="${HARBOR_REF:-v0.24.0}"
TAU3_DOMAINS="${TAU3_DOMAINS:-banking_knowledge}"
TAU3_LIMIT="${TAU3_LIMIT:-}"
# tau2-bench 版本：Harbor 0.24 的 tau3 判定器用的是 1.x 的评估接口（2.0.0 起不兼容）
export TAU2_REF="${TAU2_REF:-v1.0.1}"
SRC="$HARBOR_HOME/src"
DATASET="$HARBOR_HOME/datasets/tau3-bench"

say() { printf '\n\033[1;34m== %s\033[0m\n' "$*"; }
[ -f "$HARBOR_HOME/env.sh" ] || { echo "先运行 scripts/setup_harbor.sh" >&2; exit 1; }
# shellcheck disable=SC1091
source "$HARBOR_HOME/env.sh"

say "安装 arena 评测依赖（Harbor + Strands）"
(cd "$REPO/arena" && uv sync --frozen)

say "获取 Harbor adapter（${HARBOR_REF}）与 tau2-bench（${TAU2_REF}）"
mkdir -p "$SRC"
[ -d "$SRC/harbor" ] || git clone --depth 1 --branch "$HARBOR_REF" https://github.com/laude-institute/harbor.git "$SRC/harbor"
if [ "$(cat "$SRC/tau2-bench/.arena_ref" 2>/dev/null)" != "$TAU2_REF" ]; then
  # 仓库约 450 MB（主要是各域数据）；只检出生成任务需要的目录
  rm -rf "$SRC/tau2-bench.partial" "$SRC/tau2-bench"   # 清掉上次未完成的下载
  git clone --depth 1 --branch "$TAU2_REF" --filter=blob:none --sparse https://github.com/sierra-research/tau2-bench.git "$SRC/tau2-bench.partial"
  sparse=(src)
  IFS=, read -r -a doms <<<"$TAU3_DOMAINS"
  for d in "${doms[@]}"; do sparse+=("data/tau2/domains/$d"); done
  git -C "$SRC/tau2-bench.partial" sparse-checkout set "${sparse[@]}"
  echo "$TAU2_REF" >"$SRC/tau2-bench.partial/.arena_ref"
  mv "$SRC/tau2-bench.partial" "$SRC/tau2-bench"
fi

say "生成任务：$TAU3_DOMAINS → $DATASET"
ADAPTER="$SRC/harbor/adapters/tau3-bench/src"
(cd "$REPO/arena" && TAU2_BENCH_ROOT="$SRC/tau2-bench" PYTHONPATH="$ADAPTER" uv run --frozen python - "$DATASET" "$TAU3_DOMAINS" "$TAU3_LIMIT" <<'PY'
import sys
from pathlib import Path
from tau3_bench.adapter import Tau3BenchAdapter
out, domains, limit = sys.argv[1], sys.argv[2], sys.argv[3]
Tau3BenchAdapter(Path(out), overwrite=True, limit=int(limit) if limit else None, domains=domains).run()
PY
)

say "补丁：让模拟用户和判定器可以走 Bedrock（只传短期 Bedrock token）"
(cd "$REPO/arena" && uv run --frozen python -m arena_agents.patch_tau3_tasks "$DATASET")

n=$(find "$DATASET" -maxdepth 2 -name task.toml | wc -l | tr -d ' ')
echo
echo "完成：$n 个任务在 $DATASET"
find "$DATASET" -mindepth 1 -maxdepth 1 -type d | sort | head -5 | sed 's/^/  /'
echo "下一步：bash scripts/run_tau3_baseline.sh"
