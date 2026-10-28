#!/usr/bin/env bash
# =============================================================================
# project-squirrel -- Servers/autodeploy.sh
#
# The deploy watcher (issue #95; role-generalized in #303): a long-lived
# loop that checks the configured deploy branch (MERLE_DEPLOY_BRANCH --
# default main; stations ride the aviary-station-stable release channel,
# #354) every MERLE_DEPLOY_INTERVAL_S and brings this box current when it
# moves -- pull, restart the units listed in
# MERLE_DEPLOY_UNITS, and (where MERLE_DEPLOY_MCC=1) rebuild + restart the
# web app when the merge touched web/. Merging a PR IS the deploy; nobody
# SSHes anywhere -- which is the whole fleet story of epic #212 principle 2:
# main must stay deployable for boxes we can't walk over to.
#
# One script, several unit names (#303's call, documented): orchid runs this
# as `aviary-autodeploy`, stations as `station-autodeploy` with the unit list
# arriving from the station env file (EnvironmentFile=/home/todd/station.env,
# so the ROLE owns the list -- station.env.example documents it). The loop
# itself is this one file on every Aviary box; two forks of it in one repo is
# the outcome #303 exists to avoid. One copy PER REPO is fine (#627 D10):
# squirrel keeps its own for what still deploys from squirrel (orchid's
# `merle-autodeploy` beside this one), as MERLE keeps its own.
#
# Pull-based on purpose: a webhook receiver would need an inbound hole
# through the gateway -- no LAN this runs on has ever had one, and a station
# on someone else's network never will. A 60s poll is invisible traffic and
# merge-to-live in under a minute.
#
# The repo is PUBLIC (#627 D9), so every box clones and fetches it over
# anonymous https (https://github.com/reclinerhead/aviary.git): no deploy
# key, no credential of any kind on a box -- the guest posture a station
# demands, taken as far as it goes. The flip side is the 2026-07-27 lesson
# (pearl sat silently unfetchable at #295 for days when squirrel went
# private): if this repo is ever made private, every box's fetch fails and
# this loop logs one line and retries quietly forever. A visibility change
# is a fleet event.
#
# A LOOP SERVICE, not a systemd timer: a timer firing a oneshot every minute
# writes start/finish journal lines forever -- thousands of lines a day
# saying "nothing happened", the disease issue #35 cured in the MCC proxy.
# This loop logs ONLY when it acts (or something breaks), so
# `journalctl -u aviary-autodeploy` (or `-u station-autodeploy`) reads as a
# deploy history.
#
# Remote-failure posture (#303 -- a station is two hours from a keyboard):
# deploys touch exactly two things, the checkout and systemctl restart.
# Never earl.db, clips, or the station env file (all live outside the repo
# or untracked -- a pull cannot reach them), and never sshd/tailscaled (not
# in any MERLE_DEPLOY_UNITS), so a deploy that breaks a service leaves the
# box reachable and the failure in the journal; the restart loop below logs
# and continues rather than dying with the unit it restarted.
#
# Root/user split: the unit runs as ROOT -- the whole point is restarting
# units without a sudo password -- but every git and pnpm step is demoted to
# MERLE_DEPLOY_USER: root-owned files in the checkout or .next/ would
# silently break the next manual deploy. Demotion is setpriv, NOT runuser or
# sudo: those open a PAM session per call, and at ~3 calls per quiet tick
# that was ~7k "session opened/closed" journal lines a day -- the #35 spam
# disease sneaking back in through PAM. setpriv drops uid/gid with no PAM at
# all, and --reset-env gives the demoted step the owner's HOME (so `bash -l`
# finds the right profile, and pnpm the right PATH).
#
# Config (env -- a drop-in on orchid's aviary-autodeploy unit, the station
# env file on stations):
#   MERLE_DEPLOY_UNITS       space-separated units to restart on any main
#                            change (orchid: the Aviary's Python services;
#                            stations: earl-listener earl-sightings
#                            willard-weather earl-enrichment). Empty =
#                            restart nothing.
#   MERLE_DEPLOY_MCC         "1" where the box serves the web app (orchid and
#                            every station): when the pulled range touches
#                            web/, run install -> build -> restart for
#                            aviary-web (the deploy-web.sh order; a failed
#                            build never restarts -- old code keeps serving).
#                            The name predates the web/ rename and stays:
#                            MERLE_* names don't change (#627).
#   MERLE_DEPLOY_INTERVAL_S  poll cadence, default 60
#   MERLE_DEPLOY_BRANCH      the branch this box deploys from, default main
#                            (orchid never sets it). Stations set
#                            aviary-station-stable -- the release channel
#                            (#354): fast-forward-only, never committed to
#                            directly, promoted to main's tip once nightly
#                            by the promote-stations Action, so remote
#                            houses build in the small hours and absorb at
#                            most a day of changes. The checkout must sit ON
#                            the configured branch; anywhere else reads as
#                            pinned (see the branch guard).
#   MERLE_DEPLOY_USER        checkout owner, default todd
#   MERLE_REPO               checkout path, default /home/todd/aviary
#
# Manual escape hatches: `systemctl stop <the watcher unit>` pauses it
# (manual pulls and Servers/deploy-web.sh keep working exactly as before);
# `autodeploy.sh --once` runs a single tick by hand, no loop. And the
# SANCTIONED way to pin a box for field-testing unmerged work is simply to
# check out a branch: the branch guard in tick() pauses deploys while the
# checkout is off the configured deploy branch (transition-logged, issue
# #392) and resumes them the moment it's checked back out -- no watcher
# stop/start to remember.
#
# Deliberately NOT `set -e`: the loop must survive a failed fetch, restart,
# or build -- every step checks and logs its own failure, and the next tick
# retries. Failure never leaves a service on half-deployed code: the pull is
# --ff-only or nothing, and the web build completes before its restart.
# =============================================================================

set -uo pipefail

REPO="${MERLE_REPO:-/home/todd/aviary}"
SELF="$REPO/Servers/autodeploy.sh"
UNITS="${MERLE_DEPLOY_UNITS:-}"
DEPLOY_MCC="${MERLE_DEPLOY_MCC:-0}"
INTERVAL="${MERLE_DEPLOY_INTERVAL_S:-60}"
OWNER="${MERLE_DEPLOY_USER:-todd}"
BRANCH="${MERLE_DEPLOY_BRANCH:-main}"

fetch_down=0    # fetch failures are transition-logged (the #35 convention)
off_channel=0   # off-branch checkouts too -- one line entering, one leaving
self_changed=0  # set by tick(); the loop hands over to the new copy

log() { echo "[autodeploy] $*"; }

as_owner() {
    setpriv --reuid="$OWNER" --regid="$OWNER" --init-groups --reset-env "$@"
}

repo_git() { as_owner git -C "$REPO" "$@"; }

# One Next app's gated deploy: install -> build -> restart, the deploy-web.sh
# order. Build as the owner through a login shell -- pnpm lives on the owner's
# PATH, and a root-owned .next/ would break the next manual deploy. A failed
# build never restarts: old code keeps serving, and the log says fix forward.
deploy_next_app() {
    local dir="$1" unit="$2"
    log "$dir/ changed -- install + build, then restart (the deploy-web.sh order)"
    local build_out
    if build_out=$(as_owner bash -lc \
            "cd '$REPO/$dir' && pnpm install --frozen-lockfile && pnpm build" 2>&1); then
        if systemctl restart "$unit"; then
            log "restarted $unit"
        else
            log "restart FAILED for $unit -- check: systemctl status $unit"
        fi
    else
        log "$dir build FAILED -- old build keeps serving; fix forward and merge again"
        printf '%s\n' "$build_out" | tail -n 30
    fi
}

tick() {
    self_changed=0

    # THE BRANCH GUARD (issue #392): a checkout off the configured deploy
    # branch is a box someone deliberately pinned -- field-testing unmerged
    # work is the sanctioned use. Without this guard the loop below is a
    # disaster there: HEAD never equals the deploy branch's tip, `pull
    # --ff-only` "succeeds" against the checkout's own upstream, and every
    # watched unit restarts every tick, forever. Measured on merle
    # 2026-08-02: fieldmode killed every ~60s, three park recording sessions
    # dead at 6min/0s/60s -- and paused only while the network was down, so
    # recording worked precisely when the internet didn't. Before the fetch
    # on purpose: a pinned box generates no deploy traffic at all. Checking
    # the deploy branch back out resumes deploys by itself. "Pinned" means
    # off the CONFIGURED branch: a station checkout sitting on main is a
    # half-finished channel flip, and it pauses the same way (#354).
    local branch
    branch=$(repo_git rev-parse --abbrev-ref HEAD) || return 0
    if [ "$branch" != "$BRANCH" ]; then
        if [ "$off_channel" -eq 0 ]; then
            off_channel=1
            log "checkout is on '$branch', not $BRANCH -- deploys paused until it returns"
        fi
        return 0
    fi
    if [ "$off_channel" -eq 1 ]; then
        off_channel=0
        log "back on $BRANCH -- deploys resume"
    fi

    if ! repo_git fetch --quiet origin "$BRANCH"; then
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
    remote=$(repo_git rev-parse "origin/$BRANCH") || return 0
    [ "$head" = "$remote" ] && return 0   # the quiet path: no news, no log

    # Never act on a checkout someone's mid-something in -- but only TRACKED
    # changes count. Untracked files can't be harmed by a --ff-only pull (git
    # refuses a path collision on its own), and the services' runtime state
    # (journal windows, weather history) lives beside the code as untracked
    # or ignored files -- counting those blocked pearl's deploys forever.
    # Skipping is safe: the tick retries, so cleaning the tree resumes.
    if [ -n "$(repo_git status --porcelain --untracked-files=no)" ]; then
        log "origin/$BRANCH moved to ${remote:0:9} but the checkout is dirty -- not touching it"
        return 0
    fi

    log "deploying ${head:0:9} -> ${remote:0:9} from origin/$BRANCH"
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

    # The gated expensive path: a docs-only merge never costs a Next build, so
    # the web app rebuilds only when web/ itself changed.
    if [ "$DEPLOY_MCC" = "1" ] && grep -q "^web/" <<<"$changed"; then
        deploy_next_app web aviary-web
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
    # Branch in the startup line so `journalctl -u <watcher> | head` answers
    # "why is this box not deploying" in one read (issue #392).
    log "watching origin/$BRANCH every ${INTERVAL}s -- units: [${UNITS:-none}]" \
        "mcc: $DEPLOY_MCC repo: $REPO" \
        "branch: $(repo_git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
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
