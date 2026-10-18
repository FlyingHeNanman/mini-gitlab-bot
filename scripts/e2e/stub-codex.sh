#!/usr/bin/env bash
# =============================================================================
# 桩 codex：只用于本机端到端，**不是**真的模型
# =============================================================================
# 它实现的是 `codex exec` 的**契约**（见 adapters/codex_cli.py 与 PLAN §6）：
#   * `--version` 打印版本
#   * 读 `--cd <dir>`、`-o <file>`
#   * 在 workdir 里改代码 + `git commit`（agent 该做的事）
#   * 写 `.mini-gitlab-bot/report.json`（结构化报告）
#   * 往 `-o` 写最终消息
#   * stdout 打 JSONL，并以 `turn.completed` 收尾
#
# 这样就能在**没有 LLM 端点**的机器上验证"网关 + 真 GitLab"的全部收尾逻辑
# （四条件判定 → diff 校验 → push → draft MR → 状态评论 → 标签流转）。
# 换成真 codex 时只要 `CODEX_BIN=codex`（并配好 MODEL_BASE_URL / LLM_API_KEY）。
# =============================================================================
set -euo pipefail

if [[ "${1:-}" == "--version" ]]; then
  echo "codex-cli e2e-stub"
  exit 0
fi

cd_dir=""
out_file=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[$i]}" in
    --cd) cd_dir="${args[$((i + 1))]:-}" ;;
    -o) out_file="${args[$((i + 1))]:-}" ;;
  esac
done

if [[ -z "$cd_dir" || -z "$out_file" ]]; then
  echo "stub-codex: 缺少 --cd 或 -o" >&2
  exit 2
fi

cd "$cd_dir"
mkdir -p .mini-gitlab-bot

# ---- 1) 改代码（让 diff 非空；这是四条件的第四条）----
cat > app.py <<'PY'
def parse_labels(labels):
    """把标签列表归一化：去空白、转小写、丢空项。空输入应返回 []。"""
    if not labels:
        return []
    return [str(item).strip().lower() for item in labels if str(item).strip()]
PY

# ---- 2) 结构化报告（网关读它来渲染 MR 描述）----
cat > .mini-gitlab-bot/report.json <<'JSON'
{
  "summary": "parse_labels 在空标签时返回 []（e2e 桩 agent）",
  "fixes": ["`app.py`：空标签时返回 `[]` 而不是 `None`"],
  "tests": ["（e2e 桩 agent，未新增测试）"],
  "test_command": "（e2e 未运行测试）",
  "test_summary": "not-run",
  "not_run": ["pytest（e2e 桩 agent 不跑测试）"],
  "risks": ["这是桩 agent，只验证网关侧闭环"],
  "deps": []
}
JSON

# ---- 3) 像真 agent 一样自己 commit（网关的兜底提交会因此复用这条）----
git add -A
git -c user.name="${GIT_AUTHOR_NAME:-mini-gitlab-bot}" \
    -c user.email="${GIT_AUTHOR_EMAIL:-mini-gitlab-bot@localhost}" \
    commit -qm "fix: e2e 桩 agent 的修复" || true

# ---- 4) 最终消息 + JSONL ----
printf '已修复 add() 并提交（e2e 桩 agent）' > "$out_file"
printf '{"type":"thread.started"}\n'
printf '{"type":"item.completed","item":{"type":"agent_message"}}\n'
printf '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n'
