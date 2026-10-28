#!/usr/bin/env bash
# deploy-web.sh -- THE way to deploy the Aviary's web app by hand (orchid and
# every station; it was deploy-mcc.sh until the web/ rename, #666).
#
# "git pull + systemctl restart" is not a deploy: `next start` serves the
# compiled .next/, not source, so a restart without a build re-serves the old
# code -- and a build run *after* the restart rewrites .next/ under the live
# server ("Failed to load static file"). This script enforces the one valid
# order -- pull -> install -> build -> restart -- and set -e fails loudly at
# the first broken step, so a failed build never restarts the service.
#
# Run it on the box, from anywhere:  ~/aviary/Servers/deploy-web.sh
#
# Since issue #95 this is the MANUAL path -- the autodeploy watcher
# (autodeploy.sh: aviary-autodeploy on orchid, station-autodeploy on the
# stations) runs this same pull -> install -> build -> restart order on its
# own when MERLE_DEPLOY_MCC=1. Keep the two in step: the order rule lives
# here, and the watcher mirrors it.

set -euo pipefail

REPO="${MERLE_REPO:-$HOME/aviary}"

echo "==> git pull ($REPO)"
git -C "$REPO" pull --ff-only

cd "$REPO/web"

echo "==> pnpm install --frozen-lockfile"
pnpm install --frozen-lockfile

echo "==> pnpm build"
pnpm build

echo "==> sudo systemctl restart aviary-web"
sudo systemctl restart aviary-web

echo "==> aviary-web is $(systemctl is-active aviary-web)"
