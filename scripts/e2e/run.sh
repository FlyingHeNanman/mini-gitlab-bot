#!/usr/bin/env bash
# =============================================================================
# 本机端到端：真 GitLab CE + 真 git + 桩 codex → 验证"网关收尾链路"
# =============================================================================
# 验的是 M3 的判据（以及一部分 M4 的接线）：
#   webhook 触发 → 权限闸门（members/all）→ clone → 分支 → agent(桩) → 四条件
#   → diff 校验 → push → draft MR → 单条状态评论原地编辑 → 标签流转
# 以及一条负面用例：无权限用户触发必须被拒绝、且不产生任务。
#
# **桩 codex** 只替代"模型"这一段（见 stub-codex.sh）。换成真 codex：
#   CODEX_BIN=codex MODEL_BASE_URL=... LLM_API_KEY=... E2E_STUB_AGENT=0 ./run.sh
#
# 用法：
#   ./run.sh                 # 起 GitLab → 跑完整闭环 → 拆掉
#   ./run.sh --keep          # 跑完保留容器（下一轮加 --reuse 复用）
#   ./run.sh --reuse         # 复用已在跑的 GitLab，不重新拉/起
#   ./run.sh --with-runner   # 额外注册 runner 并验 CI 数据（M4 的工具依赖它）
#
# 环境变量（都有默认值）：
#   E2E_PORT=8929  E2E_BOT_PORT=8580  E2E_GITLAB_IMAGE=gitlab/gitlab-ce:latest
#   E2E_ROOT_PASSWORD=Kx7mQ2vT9pL4nR8（必须避开 GitLab 的弱口令库）  E2E_GITLAB_TIMEOUT=900  E2E_TASK_TIMEOUT=300
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RUNTIME_DIR="$REPO_ROOT/.e2e"
COMPOSE=(docker compose -p mgb-e2e -f "$SCRIPT_DIR/docker-compose.yml")

E2E_PORT="${E2E_PORT:-8929}"
E2E_BOT_PORT="${E2E_BOT_PORT:-8580}"
E2E_GITLAB_IMAGE="${E2E_GITLAB_IMAGE:-gitlab/gitlab-ce:17.4.2-ce.0}"
E2E_ROOT_PASSWORD="${E2E_ROOT_PASSWORD:-Kx7mQ2vT9pL4nR8}"
E2E_GITLAB_TIMEOUT="${E2E_GITLAB_TIMEOUT:-900}"
E2E_TASK_TIMEOUT="${E2E_TASK_TIMEOUT:-300}"
E2E_STUB_AGENT="${E2E_STUB_AGENT:-1}"

GITLAB="http://127.0.0.1:${E2E_PORT}"
# GitLab 容器回调宿主机：优先用 docker bridge 网关（host.docker.internal 在部分环境解析成
# IPv6 ULA，uvicorn 只监听 IPv4，会连不上 —— 实测踩过）
WEBHOOK_HOST="${E2E_WEBHOOK_HOST:-$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)}"
WEBHOOK_HOST="${WEBHOOK_HOST:-host.docker.internal}"
GROUP_PATH="bot-e2e"
PROJECT_PATH="demo"
PROJECT_FULL="$GROUP_PATH/$PROJECT_PATH"
ALICE="alice"
MALLORY="mallory"
BOT_USER="mini-gitlab-bot"
WEBHOOK_SECRET="e2e-webhook-secret-0123456789"

KEEP=0
REUSE=0
WITH_RUNNER=0
for arg in "$@"; do
  case "$arg" in
    --keep) KEEP=1 ;;
    --reuse) REUSE=1 ;;
    --with-runner) WITH_RUNNER=1 ;;
    -h | --help) sed -n '2,30p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

# ---------------------------------------------------------------- 输出与断言
if [[ -t 1 ]]; then
  C_RED=$'\033[31m' C_GRN=$'\033[32m' C_YEL=$'\033[33m' C_DIM=$'\033[2m' C_OFF=$'\033[0m'
else
  C_RED='' C_GRN='' C_YEL='' C_DIM='' C_OFF=''
fi

PASS=0
FAIL=0
SKIP=0
step() { printf '\n%s▶ %s%s\n' "$C_DIM" "$*" "$C_OFF"; }
ok() {
  printf '  %sPASS%s %s\n' "$C_GRN" "$C_OFF" "$*"
  PASS=$((PASS + 1))
}
bad() {
  printf '  %sFAIL%s %s\n' "$C_RED" "$C_OFF" "$*"
  FAIL=$((FAIL + 1))
}
skip() {
  printf '  %sSKIP%s %s\n' "$C_YEL" "$C_OFF" "$*"
  SKIP=$((SKIP + 1))
}
die() {
  printf '\n%s✖ %s%s\n' "$C_RED" "$*" "$C_OFF" >&2
  exit 1
}

json_get() { python3 -c "import json,sys;d=json.load(sys.stdin);print($1)"; }

# 轮询直到谓词成功（谓词是一个命令，成功返回 0）
wait_until() {
  local desc="$1" timeout="$2"
  shift 2
  local deadline=$((SECONDS + timeout))
  while ((SECONDS < deadline)); do
    if "$@" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  printf '  %s超时%s 等待 %s（%ss）\n' "$C_YEL" "$C_OFF" "$desc" "$timeout" >&2
  return 1
}

# ---------------------------------------------------------------- 清理
BOT_PID=''
cleanup() {
  local code=$?
  if [[ -n "$BOT_PID" ]] && kill -0 "$BOT_PID" 2>/dev/null; then
    kill "$BOT_PID" 2>/dev/null || true
    wait "$BOT_PID" 2>/dev/null || true
  fi
  if ((KEEP == 0)) && ((REUSE == 0)); then
    step "清理：docker compose down -v"
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  elif ((KEEP == 1)); then
    printf '\n%s容器保留中%s：GitLab=%s  bot 日志=%s/bot.log\n' \
      "$C_DIM" "$C_OFF" "$GITLAB" "$RUNTIME_DIR"
    printf '下一轮用 --reuse 复用；彻底清理：docker compose -p mgb-e2e -f %s down -v\n' \
      "$SCRIPT_DIR/docker-compose.yml"
  fi
  exit "$code"
}
trap cleanup EXIT

# ---------------------------------------------------------------- 0. 前置检查
step "0/8 前置检查"
for cmd in docker curl python3 git; do
  command -v "$cmd" >/dev/null || die "缺少命令：$cmd"
done
docker info >/dev/null 2>&1 || die "docker 不可用（docker info 失败）"
command -v uv >/dev/null || die "缺少 uv（准备阶段要 uv sync；装一个：curl -LsSf https://astral.sh/uv/install.sh | sh）"
mem_gb=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
((mem_gb >= 6)) || printf '  %s警告：内存只有 %sGB，GitLab CE 建议 ≥8GB（4GB 也能跑但慢）%s\n' \
  "$C_YEL" "$mem_gb" "$C_OFF"
printf '  docker=%s  mem=%sGB  镜像=%s\n' \
  "$(docker --version | awk '{print $3}' | tr -d ,)" "$mem_gb" "$E2E_GITLAB_IMAGE"

mkdir -p "$RUNTIME_DIR"
rm -rf "$RUNTIME_DIR/db" "$RUNTIME_DIR/work" "$RUNTIME_DIR/logs"
: >"$RUNTIME_DIR/bot.log"

# ---------------------------------------------------------------- 1. 起 GitLab
step "1/8 启动 GitLab CE（首次需要 3–6 分钟）"
if ((REUSE == 1)); then
  curl -fsS -o /dev/null "$GITLAB/users/sign_in" || die "--reuse 指定了，但 $GITLAB 没就绪"
  printf '  复用已就绪的实例：%s\n' "$GITLAB"
else
  E2E_GITLAB_IMAGE="$E2E_GITLAB_IMAGE" E2E_ROOT_PASSWORD="$E2E_ROOT_PASSWORD" \
    "${COMPOSE[@]}" up -d gitlab >/dev/null || die "docker compose up 失败"
fi

cat >"$RUNTIME_DIR/wait-ready.sh" <<'WAIT'
#!/usr/bin/env bash
curl -fsS -o /dev/null "${GITLAB}/users/sign_in"
WAIT
chmod +x "$RUNTIME_DIR/wait-ready.sh"
GITLAB="$GITLAB" "$RUNTIME_DIR/wait-ready.sh" || true

step "1/8 等待 GitLab 就绪（最多 ${E2E_GITLAB_TIMEOUT}s）"
if ! GITLAB="$GITLAB" wait_until "GitLab readiness" "$E2E_GITLAB_TIMEOUT" "$RUNTIME_DIR/wait-ready.sh"; then
  docker logs --tail 40 mgb-e2e-gitlab 2>&1 | tail -40 || true
  die "GitLab 没能在 ${E2E_GITLAB_TIMEOUT}s 内就绪"
fi
printf '  %s就绪%s %s\n' "$C_GRN" "$C_OFF" "$GITLAB"

# ---------------------------------------------------------------- 2. root token
step "2/8 创建 root token（容器内 rails runner）"
ADMIN_TOKEN=$(docker exec mgb-e2e-gitlab gitlab-rails runner '
  require "securerandom"
  user = User.find_by_username("root")
  raw = "glpat-" + SecureRandom.alphanumeric(20)
  token = user.personal_access_tokens.new(scopes: [:api, :read_api, :sudo],
                                          name: "e2e-#{SecureRandom.hex(4)}",
                                          expires_at: 1.day.from_now)
  token.set_token(raw)
  token.save!
  puts raw
' 2>/dev/null | grep -o 'glpat-[A-Za-z0-9_-]*' | tail -1)
[[ -n "$ADMIN_TOKEN" ]] || die "创建 root token 失败（看：docker exec mgb-e2e-gitlab gitlab-rails runner 'puts User.count'）"
printf '  root token: %s…\n' "${ADMIN_TOKEN:0:12}"

api() { # api METHOD PATH [curl args...]
  local method="$1" path="$2"
  shift 2
  curl -sS --fail-with-body -X "$method" -H "PRIVATE-TOKEN: $ADMIN_TOKEN" \
    -H "Content-Type: application/json" "$GITLAB/api/v4$path" "$@"
}
api_as() { # api_as USERNAME METHOD PATH [curl args...]
  local who="$1" method="$2" path="$3"
  shift 3
  curl -sS --fail-with-body -X "$method" -H "PRIVATE-TOKEN: $ADMIN_TOKEN" -H "Sudo: $who" \
    -H "Content-Type: application/json" "$GITLAB/api/v4$path" "$@"
}

api_version=$(api GET /version | json_get 'd["version"]' 2>/dev/null || true)
printf '  GitLab 版本：%s\n' "${api_version:-未知}"

# ---------------------------------------------------------------- 3. 用户与权限
step "3/8 创建用户（alice=Developer, mallory=Reporter, bot）"
create_user() { # name -> id
  local username="$1" admin="${2:-false}" existing
  existing=$(api GET "/users?username=$username" | json_get 'd[0]["id"]' 2>/dev/null || true)
  if [[ -n "$existing" && "$existing" != "None" ]]; then
    echo "$existing"
    return 0
  fi
  api POST /users \
    --data "{\"username\":\"$username\",\"name\":\"$username\",\"email\":\"$username@example.com\",\"skip_confirmation\":true,\"force_random_password\":true,\"admin\":$admin}" |
    json_get 'd["id"]'
}
ALICE_ID=$(create_user "$ALICE")
MALLORY_ID=$(create_user "$MALLORY")
BOT_ID=$(create_user "$BOT_USER")
[[ -n "$ALICE_ID" && -n "$MALLORY_ID" && -n "$BOT_ID" ]] || die "创建用户失败"

create_pat() { # user_id scope name -> token
  local uid="$1" scope="$2" name="$3"
  api POST "/users/$uid/personal_access_tokens" \
    --data "{\"name\":\"$name\",\"scopes\":[\"$scope\"],\"expires_at\":\"$(date -d '+30 days' +%F)\"}" |
    json_get 'd["token"]'
}
BOT_TOKEN=$(create_pat "$BOT_ID" api bot-api)
BOT_RO_TOKEN=$(create_pat "$BOT_ID" read_api bot-read)
[[ -n "$BOT_TOKEN" && -n "$BOT_RO_TOKEN" ]] || die "创建 bot token 失败"

# ---------------------------------------------------------------- 4. 项目与仓库
step "4/8 创建 group / project 并播种仓库"
GROUP_ID=$(api POST /groups --data "{\"name\":\"$GROUP_PATH\",\"path\":\"$GROUP_PATH\"}" |
  json_get 'd["id"]' 2>/dev/null || true)
if [[ -z "$GROUP_ID" || "$GROUP_ID" == "None" ]]; then
  GROUP_ID=$(api GET "/groups?search=$GROUP_PATH" | json_get 'd[0]["id"]')
fi
[[ -n "$GROUP_ID" ]] || die "创建 group 失败"

PROJECT_ID=$(api POST /projects \
  --data "{\"name\":\"$PROJECT_PATH\",\"path\":\"$PROJECT_PATH\",\"namespace_id\":$GROUP_ID,\"visibility\":\"private\"}" |
  json_get 'd["id"]' 2>/dev/null || true)
if [[ -z "$PROJECT_ID" || "$PROJECT_ID" == "None" ]]; then
  PROJECT_ID=$(api GET "/projects/$GROUP_PATH%2F$PROJECT_PATH" | json_get 'd["id"]')
fi
[[ -n "$PROJECT_ID" ]] || die "创建 project 失败"
printf '  group=%s project=%s（id=%s）\n' "$GROUP_PATH" "$PROJECT_PATH" "$PROJECT_ID"

# 成员：alice 与 bot 都是 Developer(30)；mallory 故意不加
api POST "/groups/$GROUP_ID/members" --data "{\"user_id\":$ALICE_ID,\"access_level\":30}" >/dev/null 2>&1 || true
api POST "/groups/$GROUP_ID/members" --data "{\"user_id\":$BOT_ID,\"access_level\":30}" >/dev/null 2>&1 || true
# mallory 是 Reporter(20)：能看项目、能评论，但低于闸门要求的 Developer(30)
api POST "/groups/$GROUP_ID/members" --data "{\"user_id\":$MALLORY_ID,\"access_level\":20}" >/dev/null 2>&1 || true
printf '  alice / bot = Developer(30)，mallory = Reporter(20)（低于闸门）\n'

# 播种仓库：pyproject + uv.lock（离线可 sync）+ app.py + .gitlab-ci.yml
SEED_DIR=$(mktemp -d)
cp "$SCRIPT_DIR/../.." /dev/null 2>/dev/null || true
cat >"$SEED_DIR/pyproject.toml" <<'TOML'
[project]
name = "demo"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["pytest>=8"]
TOML
cat >"$SEED_DIR/app.py" <<'PY'
def parse_labels(labels):
    """把标签列表归一化：去空白、转小写、丢空项。空输入应返回 []。"""
    if not labels:
        return None
    return [str(item).strip().lower() for item in labels if str(item).strip()]
PY
cat >"$SEED_DIR/test_labels.py" <<'PY'
from app import parse_labels


def test_empty_labels():
    assert parse_labels([]) == []


def test_normalizes():
    assert parse_labels([" Bug ", "AI-FIX"]) == ["bug", "ai-fix"]
PY
cat >"$SEED_DIR/.gitlab-ci.yml" <<'YML'
stages: [test]

e2e-job:
  stage: test
  script:
    - echo "e2e: this job intentionally fails"
    - exit 1
YML
(cd "$SEED_DIR" && UV_CACHE_DIR="${UV_CACHE_DIR:-$REPO_ROOT/.uv-cache}" uv lock -q) ||
  die "uv lock 失败（种子仓库需要 uv.lock 才能离线 sync）"

python3 - "$SEED_DIR" "$GITLAB" "$ADMIN_TOKEN" "$PROJECT_ID" <<'PYEOF'
import json, os, sys, urllib.error, urllib.request

seed, gitlab, token, project_id = sys.argv[1:5]
FILES = ("pyproject.toml", "uv.lock", "app.py", "test_labels.py", ".gitlab-ci.yml")


def call(method, path, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{gitlab}/api/v4{path}", data=body, method=method,
        headers={"PRIVATE-TOKEN": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise SystemExit(f"{method} {path} -> HTTP {exc.code}: {detail}")


# 幂等播种：已有文件用 update，新文件用 create（否则二次运行会 400）
existing = set()
try:
    tree = call("GET", f"/projects/{project_id}/repository/tree?ref=main&recursive=true&per_page=100")
    existing = {item["path"] for item in tree if item["type"] == "blob"}
except SystemExit:
    existing = set()  # 还没有 main 分支

actions = []
for name in FILES:
    with open(os.path.join(seed, name), encoding="utf-8") as fh:
        actions.append({
            "action": "update" if name in existing else "create",
            "file_path": name,
            "content": fh.read(),
        })
commit = call("POST", f"/projects/{project_id}/repository/commits", {
    "branch": "main",
    "commit_message": "chore: 初始化/刷新 demo 项目（e2e 播种）",
    "actions": actions,
})
print("  种子提交：", commit["short_id"], f"（{len(actions)} 个文件）")
PYEOF
[[ $? -eq 0 ]] || die "播种仓库失败"
rm -rf "$SEED_DIR"

# 清场：关掉遗留的 ai-fix MR、删掉遗留分支（让重跑结果可复现）
for mr_iid in $(api GET "/projects/$PROJECT_ID/merge_requests?state=opened&per_page=100" |
  json_get '" ".join(str(m["iid"]) for m in d if str(m["source_branch"]).startswith("ai-fix/"))' 2>/dev/null); do
  api PUT "/projects/$PROJECT_ID/merge_requests/$mr_iid" --data '{"state_event":"close"}' >/dev/null 2>&1 || true
done
for branch in $(api GET "/projects/$PROJECT_ID/repository/branches?per_page=100" |
  json_get '" ".join(b["name"] for b in d if b["name"].startswith("ai-fix/"))' 2>/dev/null); do
  # 分支名里的 / 必须编码，否则 DELETE 落到 404（之前就踩了这个坑）
  api DELETE "/projects/$PROJECT_ID/repository/branches/${branch//\//%2F}" >/dev/null 2>&1 || true
done

# 标签
for label in "ai-fix" "ai-fix:running" "ai-fix:done" "ai-fix:failed"; do
  api POST "/projects/$PROJECT_ID/labels" \
    --data "{\"name\":\"$label\",\"color\":\"#428BCA\"}" >/dev/null 2>&1 || true
done
printf '  标签：ai-fix / ai-fix:running / ai-fix:done / ai-fix:failed\n'

# webhook 指回宿主机：GitLab 默认禁止 webhook 打本地/内网地址，必须先打开
api PUT "/application/settings?allow_local_requests_from_web_hooks_and_services=true" >/dev/null ||
  die "无法开启 allow_local_requests_from_web_hooks_and_services（需要 admin token）"
api POST "/projects/$PROJECT_ID/hooks" \
  --data "{\"url\":\"http://${WEBHOOK_HOST}:${E2E_BOT_PORT}/webhook/gitlab\",\"token\":\"$WEBHOOK_SECRET\",\"issues_events\":true,\"note_events\":true,\"enable_ssl_verification\":false}" \
  >/dev/null || die "创建 webhook 失败"
printf '  webhook → http://%s:%s/webhook/gitlab\n' "$WEBHOOK_HOST" "$E2E_BOT_PORT"

# ---------------------------------------------------------------- 5. 起 bot
step "5/8 启动网关（宿主进程，生产装配）"
CODEX_BIN="$SCRIPT_DIR/stub-codex.sh"
if [[ "$E2E_STUB_AGENT" != "1" ]]; then
  CODEX_BIN="$(command -v codex || true)"
  [[ -n "$CODEX_BIN" ]] || die "E2E_STUB_AGENT=0 但找不到 codex"
  [[ -n "${MODEL_BASE_URL:-}" ]] || die "E2E_STUB_AGENT=0 需要 MODEL_BASE_URL"
  [[ -n "${MODEL_NAME:-}" ]] || die "E2E_STUB_AGENT=0 需要 MODEL_NAME"
  [[ -n "${LLM_API_KEY:-}" ]] || die "E2E_STUB_AGENT=0 需要 LLM_API_KEY"
  printf '  真 agent：codex=%s model=%s base=%s\n' "$CODEX_BIN" "$MODEL_NAME" "$MODEL_BASE_URL"
else
  printf '  桩 agent：%s（换真 codex：E2E_STUB_AGENT=0 + MODEL_BASE_URL/MODEL_NAME/LLM_API_KEY）\n' "$CODEX_BIN"
fi

(
  cd "$REPO_ROOT" || exit 1
  WEBHOOK_SECRET="$WEBHOOK_SECRET" \
  REPO_WHITELIST="$PROJECT_FULL" \
  BOT_USERNAME="$BOT_USER" \
  MIN_ACCESS_LEVEL=30 \
  ALLOWED_TRIGGER_USERS="" \
  GITLAB_URL="$GITLAB" \
  GITLAB_TOKEN="$BOT_TOKEN" \
  GITLAB_READONLY_TOKEN="$BOT_RO_TOKEN" \
  DB_PATH="$RUNTIME_DIR/db/tasks.db" \
  WORK_DIR="$RUNTIME_DIR/work" \
  LOG_DIR="$RUNTIME_DIR/logs" \
  LOG_FORMAT=text \
  LOG_LEVEL=INFO \
  CODEX_BIN="$CODEX_BIN" \
  MODEL_PROVIDER="${MODEL_PROVIDER:-internal}" \
  MODEL_BASE_URL="${MODEL_BASE_URL:-}" \
  MODEL_NAME="${MODEL_NAME:-e2e-model}" \
  LLM_API_KEY="${LLM_API_KEY:-}" \
  CODEX_EXPECTED_VERSION="${CODEX_EXPECTED_VERSION:-e2e-stub}" \
  AGENT_TIMEOUT_SEC="${E2E_AGENT_TIMEOUT:-600}" \
  POLL_INTERVAL_SEC=0.5 \
  LEASE_SECONDS=60 \
  WORKER_OWNER=e2e-worker \
  UV_CACHE_DIR="${UV_CACHE_DIR:-$REPO_ROOT/.uv-cache}" \
  UV_DEFAULT_INDEX="" \
  MCP_ENABLED=true \
    uv run --no-sync python -m uvicorn gateway.app:app --host 0.0.0.0 --port "$E2E_BOT_PORT" \
    >>"$RUNTIME_DIR/bot.log" 2>&1
) &
BOT_PID=$!

if ! wait_until "网关 /healthz" 90 bash -c "curl -fsS -o /dev/null http://127.0.0.1:$E2E_BOT_PORT/healthz"; then
  tail -40 "$RUNTIME_DIR/bot.log" >&2 || true
  die "网关没能起来（日志：$RUNTIME_DIR/bot.log）"
fi
printf '  %s网关就绪%s pid=%s 端口=%s\n' "$C_GRN" "$C_OFF" "$BOT_PID" "$E2E_BOT_PORT"
ready=$(curl -fsS "http://127.0.0.1:$E2E_BOT_PORT/readyz" || true)
printf '  /readyz → %s\n' "$ready"

# ---------------------------------------------------------------- 6. 触发
step "6/8 触发：alice 给 issue 打 ai-fix 标签"
ISSUE=$(api_as "$ALICE" POST "/projects/$PROJECT_ID/issues" \
  --data '{"title":"parse_labels 在空标签时返回 None","description":"给一个没有任何标签的 issue 调用 parse_labels() 会返回 None，期望 []"}')
ISSUE_IID=$(echo "$ISSUE" | json_get 'd["iid"]')
[[ -n "$ISSUE_IID" ]] || die "创建 issue 失败"
printf '  issue #%s 已创建，现在打标签（触发 webhook）\n' "$ISSUE_IID"
api_as "$ALICE" PUT "/projects/$PROJECT_ID/issues/$ISSUE_IID" \
  --data '{"add_labels":"ai-fix"}' >/dev/null || die "打标签失败"

# ---------------------------------------------------------------- 7. 断言
step "7/8 断言闭环（最多等 ${E2E_TASK_TIMEOUT}s）"
deadline=$((SECONDS + E2E_TASK_TIMEOUT))
mr_json=""
while ((SECONDS < deadline)); do
  mr_json=$(api GET "/projects/$PROJECT_ID/merge_requests?state=opened&per_page=20")
  if [[ "$(echo "$mr_json" | json_get 'len(d)')" != "0" ]]; then
    break
  fi
  sleep 3
done

metrics=$(curl -fsS "http://127.0.0.1:$E2E_BOT_PORT/metrics" || true)

# 7.1 任务成功（网关自己的指标）
if grep -q 'mini_gitlab_bot_tasks_total{state="succeeded"} 1' <<<"$metrics"; then
  ok "任务成功（/metrics：tasks_total{succeeded}=1）"
else
  bad "任务没有成功（/metrics 里找不到 succeeded=1）"
  printf '  指标：\n%s\n' "$metrics" | sed 's/^/    /'
  tail -50 "$RUNTIME_DIR/bot.log" | sed 's/^/    /' || true
fi

# 7.2 分支已推
branches=$(api GET "/projects/$PROJECT_ID/repository/branches?search=ai-fix%2F${ISSUE_IID}-")
branch_count=$(echo "$branches" | json_get 'len(d)')
if [[ "$branch_count" != "0" ]]; then
  branch=$(echo "$branches" | json_get 'd[0]["name"]')
  ok "分支已推送：$branch"
else
  bad "没有找到 ai-fix/* 分支"
fi

# 7.3 draft MR + Closes
if [[ "$(echo "$mr_json" | json_get 'len(d)')" != "0" ]]; then
  mr_title=$(echo "$mr_json" | json_get 'd[0]["title"]')
  mr_desc=$(echo "$mr_json" | json_get 'd[0]["description"]')
  mr_iid=$(echo "$mr_json" | json_get 'd[0]["iid"]')
  [[ "$mr_title" == Draft:* ]] && ok "MR 是 draft（标题前缀）：$mr_title" ||
    bad "MR 标题没有 Draft: 前缀：$mr_title"
  grep -qi "closes #$ISSUE_IID" <<<"$mr_desc" && ok "MR 描述含 Closes #$ISSUE_IID" ||
    bad "MR 描述缺少 Closes #$ISSUE_IID"
  grep -q "\*\*摘要\*\*：" <<<"$mr_desc" && ok "MR 描述带上了 report.json 的摘要" ||
    bad "MR 描述里没有 report.json 的 summary（模型可能没写 report.json）"
else
  bad "没有开出 MR"
fi

# 7.4 MR 只能有一条（幂等）
mr_count=$(echo "$mr_json" | json_get 'len(d)')
[[ "$mr_count" == "1" ]] && ok "只开了一条 MR（幂等）" || bad "MR 数量是 $mr_count，期望 1"

# 7.5 单条状态评论原地编辑
notes_json=$(api GET "/projects/$PROJECT_ID/issues/$ISSUE_IID/notes?sort=asc&per_page=100")
python3 - "$GITLAB" "$ADMIN_TOKEN" "$PROJECT_ID" "$ISSUE_IID" <<'PY' >"$RUNTIME_DIR/notes.summary" 2>&1
import json, sys, urllib.request
gitlab, token, pid, iid = sys.argv[1:5]
req = urllib.request.Request(f"{gitlab}/api/v4/projects/{pid}/issues/{iid}/notes?sort=asc&per_page=100",
                             headers={"PRIVATE-TOKEN": token})
notes = json.loads(urllib.request.urlopen(req, timeout=30).read())
human = [n for n in notes if not n.get("system")]
print(len(human))
for n in human:
    print(n["body"][:120].replace("\n", " "))
PY
note_count=$(head -1 "$RUNTIME_DIR/notes.summary")
[[ "$note_count" == "1" ]] && ok "状态评论只有 1 条（原地编辑，没有刷屏）" ||
  bad "状态评论有 $note_count 条，期望 1"
grep -q "✅" "$RUNTIME_DIR/notes.summary" && ok "评论内容是成功态（含 ✅）" ||
  bad "评论里没有成功态标记"
grep -q "已完成" "$RUNTIME_DIR/notes.summary" && ok "评论标题是「已完成」" || true

# 7.6 标签流转
labels_json=$(api GET "/projects/$PROJECT_ID/issues/$ISSUE_IID")
labels=$(echo "$labels_json" | json_get '"|".join(d["labels"])')
case "$labels" in
  *"ai-fix:done"*) ok "标签已流转到 ai-fix:done（当前：$labels）" ;;
  *) bad "标签没有到 ai-fix:done（当前：$labels）" ;;
esac
case "$labels" in
  *"ai-fix:running"*) bad "ai-fix:running 没有被移除（当前：$labels）" ;;
  *) ok "ai-fix:running 已被移除" ;;
esac

# 7.7 工作目录已清理、日志留存
if ls "$RUNTIME_DIR/logs"/*/codex.jsonl >/dev/null 2>&1; then
  ok "codex 日志已留存（$RUNTIME_DIR/logs/<task>/codex.jsonl）"
else
  bad "没有找到 codex 日志"
fi
if [[ -z "$(ls -A "$RUNTIME_DIR/work" 2>/dev/null | grep '^task-' || true)" ]]; then
  ok "job 工作目录已在终态清理"
else
  bad "job 工作目录没有清理干净"
fi

# 7.8 负面用例：非成员触发必须被拒
step "7b 负面用例：mallory（Reporter=20 < Developer=30）评论 /ai-fix"
before=$(grep -c 'mini_gitlab_bot_tasks_total{state=' <<<"$metrics" || true)
api_as "$MALLORY" POST "/projects/$PROJECT_ID/issues/$ISSUE_IID/notes" \
  --data '{"body":"/ai-fix"}' >/dev/null || true
sleep 4
after_metrics=$(curl -fsS "http://127.0.0.1:$E2E_BOT_PORT/metrics" || true)
if grep -qE '权限闸门拒绝|insufficient_access_level' "$RUNTIME_DIR/bot.log"; then
  ok "低权限用户触发被权限闸门拒绝（日志里有 permission_denied）"
else
  bad "日志里没有权限拒绝记录"
fi
if grep -q 'tasks_total{state="succeeded"} 1' <<<"$after_metrics" &&
  ! grep -q 'tasks_total{state="succeeded"} 2' <<<"$after_metrics"; then
  ok "被拒绝的触发没有产生新任务"
else
  bad "被拒绝的触发似乎产生了新任务"
fi
printf '  %s（before=%s）%s\n' "$C_DIM" "$before" "$C_OFF"

# ---------------------------------------------------------------- 8. 可选：runner
if ((WITH_RUNNER == 1)); then
  step "8/8 注册 runner 并验证 CI 数据（M4 的工具依赖它）"
  runner=$(api POST /user/runners \
    --data "{\"runner_type\":\"project_type\",\"project_id\":$PROJECT_ID,\"description\":\"e2e\",\"tag_list\":[\"e2e\"],\"run_untagged\":true}")
  runner_token=$(echo "$runner" | json_get 'd["token"]' 2>/dev/null || true)
  if [[ -z "$runner_token" || "$runner_token" == "None" ]]; then
    skip "无法通过 API 创建 runner（版本差异），M4 的 CI 工具本次不验"
  else
    "${COMPOSE[@]}" run --rm -T runner register \
      --non-interactive --url "http://127.0.0.1:8929" --token "$runner_token" \
      --executor shell --description e2e-shell >/dev/null 2>&1 &&
      ok "runner 已注册" || skip "runner register 失败"
    "${COMPOSE[@]}" up -d runner >/dev/null 2>&1 || true
    api POST "/projects/$PROJECT_ID/pipeline" --data '{"ref":"main"}' >/dev/null 2>&1 || true
    sleep 8
    pipelines=$(api GET "/projects/$PROJECT_ID/pipelines?per_page=5")
    if [[ "$(echo "$pipelines" | json_get 'len(d)')" != "0" ]]; then
      ok "pipeline 已产生（M4 的 list_pipelines/list_pipeline_jobs 有真数据可读）"
    else
      skip "还没有 pipeline（runner 可能需要更久）"
    fi
  fi
else
  step "8/8 跳过 runner（加 --with-runner 可验证 CI 数据）"
  skip "CI 相关（pipeline/job/trace）本次不验"
fi

# ---------------------------------------------------------------- 汇总
printf '\n%s──────────────────────────────%s\n' "$C_DIM" "$C_OFF"
printf '  通过 %s%s%s  失败 %s%s%s  跳过 %s%s%s\n' \
  "$C_GRN" "$PASS" "$C_OFF" "$C_RED" "$FAIL" "$C_OFF" "$C_YEL" "$SKIP" "$C_OFF"
printf '  bot 日志：%s/bot.log\n' "$RUNTIME_DIR"
if ((FAIL > 0)); then
  printf '%s✖ 有失败项%s\n' "$C_RED" "$C_OFF"
  exit 1
fi
printf '%s✔ 全部通过%s\n' "$C_GRN" "$C_OFF"
exit 0
