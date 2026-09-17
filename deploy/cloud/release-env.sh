#!/usr/bin/env bash
# 发布配置解析 + 角色边界：所有 deploy/cloud 脚本共享这一份实现。
#
# 为什么把发布配置移出仓库
# ------------------------
# workbench.env 决定“这次发布会打到哪台 ECS”。它只含路由元数据（实例 id、
# 区域、公网地址、服务名），不含密钥；但它是发布能力的开关。留在仓库里意味着
# 任何任务 worktree、release 归档或误提交都可能带上它。正式位置改为用户配置
# 目录：
#
#   $XDG_CONFIG_HOME/trosa/workbench.env   （默认 ~/.config/trosa/workbench.env）
#
# 仓库内的旧位置仍可读取（兼容期），但会提示迁移。开发 Agent 的 worktree 不再
# 天然拥有这份配置。
#
# 角色边界（TRADE_OS_AGENT_ROLE）
# -------------------------------
#   dev     开发：可 create/adopt/test/sync/remove/commit，不能发布
#   review  审查：只读 + 可跑门禁，不能发布
#   release 发布：唯一可执行 publish/rollback
#   未设置   默认按 release 处理，保证人工操作与现有流程不被破坏
#
# 说明：同机同用户的进程无法用环境变量做硬隔离。真正的边界是文件权限——发布
# 配置放在仓库外并设为 600，开发 Agent 的 worktree 不再包含它；角色变量是给
# Agent 会话的显式护栏与清晰报错，不是加密边界。

trosa_config_home() {
  printf '%s/trosa' "${XDG_CONFIG_HOME:-$HOME/.config}"
}

# 按优先级输出候选路径（存在的在前）。$1=脚本目录，$2=主仓根（可省略）。
trosa_workbench_env_candidates() {
  local script_dir=$1 main_root=${2:-}
  local canonical
  canonical="$(trosa_config_home)/workbench.env"
  if [[ -n "${TRADE_OS_WORKBENCH_ENV:-}" ]]; then
    printf '%s\n' "$TRADE_OS_WORKBENCH_ENV"
    return 0
  fi
  printf '%s\n' "$canonical"
  [[ -n "$main_root" ]] && printf '%s\n' "$main_root/deploy/cloud/workbench.env"
  printf '%s\n' "$script_dir/workbench.env"
}

# 解析要使用的发布配置：优先显式覆盖，其次正式位置，再次兼容旧位置；
# 都不存在时返回正式位置（让报错指向迁移目标）。
trosa_resolve_workbench_env() {
  local script_dir=$1 main_root=${2:-}
  local candidate
  while IFS= read -r candidate; do
    [[ -n "$candidate" && -r "$candidate" ]] && { printf '%s' "$candidate"; return 0; }
  done < <(trosa_workbench_env_candidates "$script_dir" "$main_root")
  printf '%s' "$(trosa_config_home)/workbench.env"
}

# 旧位置提示：不阻断，只提醒迁移，避免运维脚本突然不可用。
trosa_warn_legacy_workbench_env() {
  local resolved=$1
  [[ "$resolved" == */deploy/cloud/workbench.env ]] || return 0
  printf '提示：发布配置仍在仓库内（%s）；请迁移到 %s 并设为 600。\n' \
    "$resolved" "$(trosa_config_home)/workbench.env" >&2
}

# 发布角色守卫：非 release 角色拒绝执行会改 production 的命令。
# 返回 0 = 允许；返回 1 = 拒绝（调用方负责退出）。
trosa_require_release_role() {
  local role="${TRADE_OS_AGENT_ROLE:-release}"
  case "$role" in
    release) return 0 ;;
    dev|development)
      printf '发布被拒绝：当前角色 TRADE_OS_AGENT_ROLE=%s 没有发布权限。\n' "$role" >&2
      printf '请把改动 commit 到 agent/<id> 分支，交给 release 角色发布（preflight/publish 归属不同角色）。\n' >&2
      return 1
      ;;
    review|readonly|read-only)
      printf '发布被拒绝：审查角色 TRADE_OS_AGENT_ROLE=%s 只能读取与跑门禁。\n' "$role" >&2
      return 1
      ;;
    *)
      printf '发布被拒绝：未知角色 TRADE_OS_AGENT_ROLE=%s（允许：dev / review / release）。\n' "$role" >&2
      return 1
      ;;
  esac
}

# 人类可读的当前角色，用于 status 输出。
trosa_agent_role() { printf '%s' "${TRADE_OS_AGENT_ROLE:-release}"; }
