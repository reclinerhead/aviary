#!/usr/bin/env bash
# =============================================================================
# project-squirrel -- Servers/autodeploy.sh
#
# The deploy watcher (issue #95): a long-lived loop that checks origin/main
# every MERLE_DEPLOY_INTERVAL_S and brings this box current when it moves --
# pull, restart the units listed in MERLE_DEPLOY_UNITS, and (on pearl, where
# MERLE_DEPLOY_MCC=1) rebuild + restart the MCC when the merge touched mcc/.
# Merging a PR IS the deploy; nobody SSHes anywhere.
#
# Pull-based on purpose: the repo is public, which rules out self-hosted
# GitHub Actions runners (GitHub advises against them on public repos), and a
# webhook receiver would need an inbound hole through the gateway -- this LAN
# has never had one. A 60s poll is invisible traffic and merge-to-live in
# under a minute.
#
# A LOOP SERVICE, not a systemd timer: a timer firing a oneshot every minute
# writes start/finish journal lines forever -- thousands of lines a day
# saying "nothing happened", the disease issue #35 cured in the MCC proxy.
# This loop logs ONLY when it acts (or something breaks), so
# `journalctl -u merle-autodeploy` reads as a deploy history.
#
# Root/user split: the unit runs as ROOT -- the whole point is restarting
# units without a sudo password -- but every git and pnpm step is demoted to
# MERLE_DEPLOY_USER via runuser: root-owned files in the checkout or .next/
# would silently break the next manual deploy.
#
# Config (env, set in the unit):
#   MERLE_DEPLOY_UNITS       space-separated units to restart on any main
#                            change (pearl: the three Python services;
#                            merle: narrator-jim). Empty = restart nothing.
#   MERLE_DEPLOY_MCC         "1" on pearl only: when the pulled range touches
#                            mcc/, run install -> build -> restart for
#                            mcc-dashboard (the deploy-mcc.sh order; a failed
#                            build never restarts -- old code keeps serving)
#   MERLE_DEPLOY_INTERVAL_S  poll cadence, default 60
#   MERLE_DEPLOY_USER        checkout owner, default todd
#   MERLE_REPO               checkout path, default /home/todd/project-squirrel
#
# Manual escape hatches: `systemctl stop merle-autodeploy` pauses it (manual
# pulls and Servers/deploy-mcc.sh keep working exactly as before);
# `autodeploy.sh --once` runs a single tick by hand, no loop.
#
# Deliberately NOT `set -e`: the loop must survive a failed fetch, restart,
# or build -- every step checks and logs its own failure, and the next tick
# retries. Failure never leaves a service on half-deployed code: the pull is
# --ff-only or nothing, and the MCC build completes before its restart.
# =============================================================================

set -uo pipefail

REPO="${MERLE_REPO:-/home/todd/project-squirrel}"
SELF="$REPO/Servers/autodeploy.sh"
UNITS="${MERLE_DEPLOY_UNITS:-}"
DEPLOY_MCC="${MERLE_DEPLOY_MCC:-0}"
INTERVAL="${MERLE_DEPLOY_INTERVAL_S:-60}"
OWNER="${MERLE_DEPLOY_USER:-todd}"

fetch_down=0    # fetch failures are transition-logged (the #35 convention)
self_changed=0  # set by tick(); the loop hands over to the new copy

log() { echo "[autodeploy] $*"; }

as_owner() { runuser -u "$OWNER" -- "$@"; }

repo_git() { as_owner git -C "$REPO" "$@"; }

tick() {
    self_changed=0

    if ! repo_git fetch --quiet origin main; then
        # One line when the fetch starts failing, one when it recovers --
        # never one per quiet minute of an outage.
        if [ "$fetch_down" -eq 0 ]; then
            fetch_down=1
            log "can't fetch origin (network/GitHub down?) -- retrying quietly"
        fi
        return 0
    fi
    if [ "$fetch_down" -eq 1 ]; then
        fetch_down=0
        log "fetch recovered"
    fi

    local head remote
    head=$(repo_git rev-parse HEAD) || return 0
    remote=$(repo_git rev-parse origin/main) || return 0
    [ "$head" = "$remote" ] && return 0   # the quiet path: no news, no log

    # Never act on a checkout someone's mid-something in. Skipping is safe:
    # the tick retries forever, so cleaning the tree resumes deploys.
    if [ -n "$(repo_git status --porcelain)" ]; then
        log "origin/main moved to ${remote:0:9} but the checkout is dirty -- not touching it"
        return 0
    fi

    log "deploying ${head:0:9} -> ${remote:0:9}"
    if ! repo_git pull --ff-only --quiet; then
        log "pull --ff-only refused (diverged history?) -- needs a human"
        return 0
    fi

    local changed
    changed=$(repo_git diff --name-only "$head" "$remote")

    local unit
    for unit in $UNITS; do
        if systemctl restart "$unit"; then
            log "restarted $unit"
        else
            log "restart FAILED for $unit -- check: systemctl status $unit"
        fi
    done

    # The gated expensive path (pearl only): a docs-only merge never costs a
    # Next build. Build as the owner through a login shell -- pnpm lives on
    # the owner's PATH, and a root-owned .next/ would break manual deploys.
    if [ "$DEPLOY_MCC" = "1" ] && grep -q "^mcc/" <<<"$changed"; then
        log "mcc/ changed -- install + build, then restart (the deploy-mcc.sh order)"
        local build_out
        if build_out=$(as_owner bash -lc \
                "cd '$REPO/mcc' && pnpm install --frozen-lockfile && pnpm build" 2>&1); then
            if systemctl restart mcc-dashboard; then
                log "restarted mcc-dashboard"
            else
                log "restart FAILED for mcc-dashboard -- check: systemctl status mcc-dashboard"
            fi
        else
            log "MCC build FAILED -- old build keeps serving; fix forward and merge again"
            printf '%s\n' "$build_out" | tail -n 30
        fi
    fi

    # The self-update guard: this script deploys the repo it lives in. The
    # body runs entirely from functions parsed at startup, so the pulled copy
    # can't corrupt this run -- the loop exec's the new file before sleeping.
    if grep -q "^Servers/autodeploy.sh$" <<<"$changed"; then
        self_changed=1
    fi
    log "deploy complete at ${remote:0:9}"
}

main() {
    if [ "${1:-}" = "--once" ]; then
        tick   # a single hand-run tick: desk-testing, no loop, no self-exec
        return
    fi
    log "watching origin/main every ${INTERVAL}s -- units: [${UNITS:-none}] mcc: $DEPLOY_MCC repo: $REPO"
    while true; do
        tick
        if [ "$self_changed" -eq 1 ]; then
            log "autodeploy.sh itself changed -- handing over to the new copy"
            exec "$SELF"
        fi
        sleep "$INTERVAL"
    done
}

main "$@"
