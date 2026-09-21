#!/usr/bin/env bash
# Push this repo to https://github.com/will6443211/cursor
# Usage: GH_TOKEN=ghp_xxx ./connect_github.sh
set -euo pipefail
cd "$(dirname "$0")"

REMOTE_URL="https://github.com/will6443211/cursor.git"
TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
if [[ -z "${TOKEN}" && -f .github_token ]]; then
  TOKEN="$(tr -d '[:space:]' < .github_token)"
fi
if [[ -z "${TOKEN}" ]]; then
  echo "需要 GitHub token（repo 权限，账号 will6443211）。" >&2
  echo "  GH_TOKEN=ghp_xxx $0" >&2
  echo "或把 token 写到 $(pwd)/.github_token（已 gitignore）" >&2
  exit 1
fi

git remote remove origin 2>/dev/null || true
git remote add origin "${REMOTE_URL}"
git config http.version HTTP/1.1

export GIT_TERMINAL_PROMPT=0
push_auth() {
  git -c "http.extraHeader=Authorization: Bearer ${TOKEN}" push -u origin "$@"
}

push_auth HEAD:main
git remote set-url origin "${REMOTE_URL}"
echo "已推到 ${REMOTE_URL} 的 main"
git -c "http.extraHeader=Authorization: Bearer ${TOKEN}" ls-remote --heads origin
