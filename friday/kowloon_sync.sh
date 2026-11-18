#!/bin/bash
# Every repo of the kowloon-network organization, cloned (shallow) or
# brought up to date in ~/patti/kowloon, for ask_claude.py's Kowloon
# questions. Run hourly by kowloon-sync.timer. Needs gh logged in with the
# read-only token (gh auth login --with-token; gh auth setup-git).
set -u
ORG=kowloon-network
DIR=~/patti/kowloon
mkdir -p "$DIR"
cd "$DIR" || exit 1
for repo in $(gh repo list "$ORG" --limit 200 --json name --jq '.[].name'); do
    if [ -d "$repo/.git" ]; then
        git -C "$repo" pull -q --ff-only --depth 1 || echo "sync: $repo didn't update"
    else
        gh repo clone "$ORG/$repo" "$repo" -- -q --depth 1 || echo "sync: $repo didn't clone"
    fi
done
