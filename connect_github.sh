#!/usr/bin/env bash
# Create https://github.com/neosun100/zixuan-fenxi (private) if missing, then push.
# Usage: GH_TOKEN=ghp_xxx ./connect_github.sh
set -euo pipefail
cd "$(dirname "$0")"

OWNER=neosun100
NAME=zixuan-fenxi
REMOTE_URL="https://github.com/${OWNER}/${NAME}.git"
TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
if [[ -z "${TOKEN}" && -f .github_token ]]; then
  TOKEN="$(tr -d '[:space:]' < .github_token)"
fi
if [[ -z "${TOKEN}" ]]; then
  echo "需要 GitHub token（repo 权限）。" >&2
  echo "  GH_TOKEN=ghp_xxx $0" >&2
  echo "或把 token 写到 $(pwd)/.github_token（已 gitignore）" >&2
  exit 1
fi

AUTH=( -H "Authorization: Bearer ${TOKEN}" -H "Accept: application/vnd.github+json" )
code="$(curl -sS -o /tmp/zixuan-gh-repo.json -w '%{http_code}' "${AUTH[@]}" \
  "https://api.github.com/repos/${OWNER}/${NAME}")"
if [[ "${code}" == "404" ]]; then
  echo "创建私有仓库 ${OWNER}/${NAME} …"
  curl -sS -f "${AUTH[@]}" -X POST "https://api.github.com/user/repos" \
    -d '{"name":"'"${NAME}"'","private":true,"description":"A-share watchlist snapshot"}' \
    >/tmp/zixuan-gh-create.json
elif [[ "${code}" != "200" ]]; then
  echo "GitHub API ${code}: $(head -c 400 /tmp/zixuan-gh-repo.json)" >&2
  exit 1
fi

git remote remove origin 2>/dev/null || true
git remote add origin "${REMOTE_URL}"
git config http.version HTTP/1.1

export GIT_TERMINAL_PROMPT=0
push_auth() {
  git -c "http.extraHeader=Authorization: Bearer ${TOKEN}" push -u origin "$@"
}

push_auth master
push_auth HEAD
git remote set-url origin "${REMOTE_URL}"
echo "已推到 ${REMOTE_URL}"
git -c "http.extraHeader=Authorization: Bearer ${TOKEN}" ls-remote --heads origin
