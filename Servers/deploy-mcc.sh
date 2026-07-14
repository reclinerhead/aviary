#!/usr/bin/env bash
# deploy-mcc.sh -- THE way to deploy the MCC dashboard on pearl.
#
# "git pull + systemctl restart" is not a deploy: `next start` serves the
# compiled .next/, not source, so a restart without a build re-serves the old
# code -- and a build run *after* the restart rewrites .next/ under the live
# server ("Failed to load static file"). This script enforces the one valid
# order -- pull -> install -> build -> restart -- and set -e fails loudly at
# the first broken step, so a failed build never restarts the service.
#
# Run it on pearl, from anywhere:  ~/project-squirrel/Servers/deploy-mcc.sh
#
# Since issue #95 this is the MANUAL path -- merle-autodeploy (autodeploy.sh)
# watches origin/main and runs this same pull -> install -> build -> restart
# order on its own. Keep the two in step: the order rule lives here, and the
# watcher mirrors it.

set -euo pipefail

REPO="${MERLE_REPO:-$HOME/project-squirrel}"

echo "==> git pull ($REPO)"
git -C "$REPO" pull --ff-only

cd "$REPO/mcc"

echo "==> pnpm install --frozen-lockfile"
pnpm install --frozen-lockfile

echo "==> pnpm build"
pnpm build

echo "==> sudo systemctl restart mcc-dashboard"
sudo systemctl restart mcc-dashboard

echo "==> mcc-dashboard is $(systemctl is-active mcc-dashboard)"
