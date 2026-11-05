#!/usr/bin/env bash
# 用 Strands baseline Agent 跑 τ³-bench 任务（Harbor + Docker）。
# 前置：scripts/setup_harbor.sh、scripts/prepare_tau3.sh。
#
#   bash scripts/run_tau3_baseline.sh                 # 跑第 1 个任务
#   TASKS="tau3-banking_knowledge-001 tau3-banking_knowledge-002" bash scripts/run_tau3_baseline.sh
#   N_TASKS=5 ATTEMPTS=3 bash scripts/run_tau3_baseline.sh
#
# 环境变量：
#   MODEL        被测 Agent 的模型，默认 global.anthropic.claude-opus-4-6-v1
#   USER_SIM     bedrock（默认）或 openai：模拟用户与判定器用哪家模型
#   USER_MODEL   模拟用户/判定器模型（LiteLLM 名称）
#                bedrock 默认 bedrock/converse/global.anthropic.claude-haiku-4-5-20251001-v1:0
#                openai  默认 gpt-5.2（需要 OPENAI_API_KEY）
#   AWS_REGION   默认 us-west-2
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HARBOR_HOME="${HARBOR_HOME:-$HOME/.agent-arena/harbor}"
DATASET="$HARBOR_HOME/datasets/tau3-bench"
MODEL="${MODEL:-global.anthropic.claude-opus-4-6-v1}"
USER_SIM="${USER_SIM:-bedrock}"
AWS_REGION="${AWS_REGION:-us-west-2}"
ATTEMPTS="${ATTEMPTS:-1}"
N_TASKS="${N_TASKS:-1}"
JOBS="$HARBOR_HOME/jobs/tau3-baseline"

# shellcheck disable=SC1091
source "$HARBOR_HOME/env.sh"
[ -d "$DATASET" ] || { echo "先运行 scripts/prepare_tau3.sh" >&2; exit 1; }
cd "$REPO/arena"
export PYTHONPATH="$REPO/arena${PYTHONPATH:+:$PYTHONPATH}"   # 让 harbor 能导入 arena_agents

export OPENAI_API_KEY="${OPENAI_API_KEY:-}" OPENAI_BASE_URL="${OPENAI_BASE_URL:-}"
case "$USER_SIM" in
  bedrock)
    USER_MODEL="${USER_MODEL:-bedrock/converse/global.anthropic.claude-haiku-4-5-20251001-v1:0}"
    AWS_BEARER_TOKEN_BEDROCK="$(uv run --frozen python -m arena_agents.bedrock_token --region "$AWS_REGION")"
    export AWS_BEARER_TOKEN_BEDROCK AWS_REGION_NAME="$AWS_REGION"
    # 走 override：只传温度，不传 reasoning_effort（Claude 开思考时不允许 temperature=0）
    export TAU2_USER_LLM_ARGS_JSON='{"temperature": 0.0}'
    ;;
  openai)
    [ -n "$OPENAI_API_KEY" ] || { echo "USER_SIM=openai 需要 OPENAI_API_KEY" >&2; exit 1; }
    USER_MODEL="${USER_MODEL:-gpt-5.2}"
    ;;
  *) echo "USER_SIM 只能是 bedrock 或 openai" >&2; exit 1 ;;
esac
export TAU2_USER_MODEL="$USER_MODEL" TAU2_NL_ASSERTIONS_MODEL="$USER_MODEL"

if [ -n "${TASKS:-}" ]; then
  read -r -a task_list <<<"$TASKS"
else
  task_list=()
  while IFS= read -r t; do task_list+=("$t"); done \
    < <(find "$DATASET" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort | head -n "$N_TASKS")
fi

echo "被测模型 ${MODEL}；模拟用户/判定 ${USER_MODEL}；任务 ${#task_list[@]} 个 × $ATTEMPTS 次"
for t in "${task_list[@]}"; do
  echo; echo "== $t"
  uv run --frozen harbor run \
    -p "$DATASET/$t" \
    -a arena_agents.tau3_strands:StrandsTau3Agent \
    -m "$MODEL" \
    --ak region="$AWS_REGION" \
    -k "$ATTEMPTS" -n 1 \
    -o "$JOBS" \
    -e docker -y
done
echo; echo "结果：${JOBS}（harbor view ${JOBS}）"
