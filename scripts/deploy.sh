#!/usr/bin/env bash
# Deploys one commit of Muuvies to production.
#
# Steps: check out the commit -> ./scripts/backup_data.sh -> rebuild and restart
# only the `media-search` container -> health check. If the health check fails,
# the previous image and commit are restored and the script exits non-zero.
#
# Usage (run from the production checkout, never the dev checkout):
#   scripts/deploy.sh <40-char commit sha>   deploy that commit (must be on origin/$DEPLOY_BRANCH)
#   scripts/deploy.sh --rollback             redeploy the last commit that deployed successfully before the current one
#   scripts/deploy.sh --ssh                  SSH forced-command mode: the sha comes from $SSH_ORIGINAL_COMMAND
#   scripts/deploy.sh --status               print the deploy history
#
# One-time setup: the checkout must be marked with `git config muuvies.production true`.
# See "Deploying" in README.md.

set -euo pipefail

# Everything lives in main() so bash has parsed the whole file before `git checkout`
# can replace it on disk mid-run.
main() {
    local root
    root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    cd "${root}"

    DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"
    HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8888/api/health}"
    PUBLIC_HEALTH_URL="${PUBLIC_HEALTH_URL:-}"
    HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-120}"
    STATE_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/muuvies"
    HISTORY="${STATE_DIR}/deploys.log"
    SERVICE="media-search"
    COMPOSE=(docker compose --project-directory "${root}" -f "${root}/docker-compose.yml")

    local mode="${1:-}" target=""
    case "${mode}" in
        --ssh)
            # Only a bare commit sha is accepted from the CI key; anything else is refused.
            target="${SSH_ORIGINAL_COMMAND:-}"
            ;;
        --rollback)
            target="$(previous_good_sha)" || die "No earlier successful deploy recorded in ${HISTORY}; pass a sha instead."
            ;;
        --status)
            [ -f "${HISTORY}" ] && tail -n 20 "${HISTORY}" || echo "No deploys recorded yet."
            exit 0
            ;;
        -h|--help|"")
            sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 2
            ;;
        *)
            target="${mode}"
            ;;
    esac
    [[ "${target}" =~ ^[0-9a-f]{40}$ ]] || die "Expected a full 40-character commit sha, got '${target}'."

    preflight
    mkdir -p "${STATE_DIR}"
    exec 9>"${STATE_DIR}/deploy.lock"
    flock -w 900 9 || die "Another deploy is still running."

    local previous_sha
    previous_sha="$(git rev-parse HEAD)"

    step "Fetching origin/${DEPLOY_BRANCH}"
    git fetch --quiet --prune origin "+refs/heads/${DEPLOY_BRANCH}:refs/remotes/origin/${DEPLOY_BRANCH}"
    git cat-file -e "${target}^{commit}" 2>/dev/null || die "Commit ${target} does not exist on origin."
    git merge-base --is-ancestor "${target}" "origin/${DEPLOY_BRANCH}" \
        || die "Commit ${target} is not on origin/${DEPLOY_BRANCH}; refusing to deploy it."

    step "Backing up SQLite database and Fernet key"
    ./scripts/backup_data.sh

    # Keep the running image so a failed deploy can be undone without a rebuild.
    local image="" rollback_tag=""
    if docker inspect "${SERVICE}" >/dev/null 2>&1; then
        image="$(docker inspect -f '{{.Config.Image}}' "${SERVICE}")"
        image="${image%%:*}"
        if docker image tag "$(docker inspect -f '{{.Image}}' "${SERVICE}")" "${image}:rollback" 2>/dev/null; then
            rollback_tag="${image}:rollback"
        else
            echo "  Warning: the running image no longer exists (replaced by a manual build?), so a failed deploy cannot be rolled back automatically." >&2
        fi
    fi

    step "Checking out ${target:0:12} (was ${previous_sha:0:12})"
    git checkout --quiet --detach "${target}"

    step "Rebuilding ${SERVICE} only (caddy-proxy and seedbox-tunnel keep running)"
    if ! "${COMPOSE[@]}" up -d --build --no-deps "${SERVICE}"; then
        record "${target}" "build-failed"
        git checkout --quiet --detach "${previous_sha}"
        die "docker compose build/up failed for ${target:0:12}; source reset to ${previous_sha:0:12}. The running container was not replaced if the build failed."
    fi

    step "Health check"
    if health_check; then
        record "${target}" "ok"
        echo
        echo "Deployed ${target:0:12} ($(git log -1 --format=%s "${target}"))."
        exit 0
    fi

    record "${target}" "unhealthy"
    echo >&2
    echo "DEPLOY FAILED: ${target:0:12} did not pass the health check. Recent ${SERVICE} logs:" >&2
    docker logs --tail 40 "${SERVICE}" 2>&1 | sed 's/^/    /' >&2 || true

    if [ -z "${rollback_tag}" ]; then
        die "No previous image to roll back to. Production is DOWN or degraded; fix forward or run scripts/deploy.sh <good sha>."
    fi

    step "Rolling back to ${previous_sha:0:12}"
    git checkout --quiet --detach "${previous_sha}"
    docker image tag "${rollback_tag}" "${image}:latest"
    "${COMPOSE[@]}" up -d --no-build --no-deps "${SERVICE}"
    if health_check; then
        record "${previous_sha}" "rolled-back"
        die "Deploy of ${target:0:12} failed its health check. Rolled back to ${previous_sha:0:12}, which is healthy."
    fi
    record "${previous_sha}" "rollback-unhealthy"
    die "Deploy of ${target:0:12} failed AND the rollback to ${previous_sha:0:12} is unhealthy. Production needs manual attention."
}

preflight() {
    [ "$(git config --bool muuvies.production 2>/dev/null)" = "true" ] \
        || die "$(pwd) is not marked as the production checkout (git config muuvies.production true). Refusing to deploy from it."
    git diff --quiet HEAD -- || die "The production checkout has local changes to tracked files; refusing to overwrite them."
    [ -f .env ] || die ".env is missing from $(pwd)."
    [ -f data/media_search.db ] && [ -f data/.fernet_key ] || die "data/media_search.db or data/.fernet_key is missing from $(pwd)."
    local ssh_dir="${SEEDBOX_SSH_DIR:-${HOME}/.config/muuvies/seedbox-ssh}"
    [ -f "${ssh_dir}/id_ed25519" ] && [ -f "${ssh_dir}/known_hosts" ] \
        || die "Seedbox SSH key folder ${ssh_dir} is missing id_ed25519 or known_hosts (needed for streaming)."
    command -v docker >/dev/null || die "docker is not installed."
    command -v curl >/dev/null || die "curl is not installed."
}

# Passes when the app reports {"status":"healthy"} locally and through Caddy, and the
# container has not restarted. The response body is not printed (it holds config values).
health_check() {
    local deadline=$((SECONDS + HEALTH_TIMEOUT)) body=""
    while [ "${SECONDS}" -lt "${deadline}" ]; do
        body="$(curl -fsS --max-time 5 "${HEALTH_URL}" 2>/dev/null || true)"
        if [[ "${body}" == *'"status":"healthy"'* ]]; then
            break
        fi
        sleep 3
    done
    if [[ "${body}" != *'"status":"healthy"'* ]]; then
        if [[ "${body}" == *'"status":"degraded"'* ]]; then
            echo "  ${HEALTH_URL} reports degraded (database check failed)." >&2
        else
            echo "  ${HEALTH_URL} did not answer healthy within ${HEALTH_TIMEOUT}s." >&2
        fi
        return 1
    fi
    echo "  ${HEALTH_URL}: healthy"

    if [ -n "${PUBLIC_HEALTH_URL}" ]; then
        body="$(curl -fsS --max-time 10 --retry 3 --retry-delay 2 "${PUBLIC_HEALTH_URL}" 2>/dev/null || true)"
        if [[ "${body}" != *'"status":"healthy"'* ]]; then
            echo "  ${PUBLIC_HEALTH_URL} (through Caddy) is not healthy." >&2
            return 1
        fi
        echo "  ${PUBLIC_HEALTH_URL}: healthy"

        # The API can be healthy while the page is blank (e.g. Caddy can't
        # reach the React build). Load the page and its entry script too.
        local site_url="${PUBLIC_HEALTH_URL%/api/health}/" page entry
        page="$(curl -fsS --max-time 10 --retry 3 --retry-delay 2 "${site_url}" 2>/dev/null || true)"
        entry="$(grep -oE '/static/app/assets/[A-Za-z0-9._-]+\.js' <<<"${page}" | head -n1 || true)"
        if [ -z "${entry}" ]; then
            echo "  ${site_url} did not return the app page (no /static/app entry script)." >&2
            return 1
        fi
        if ! curl -fsS --max-time 10 --retry 3 --retry-delay 2 -o /dev/null "${site_url%/}${entry}"; then
            echo "  ${site_url%/}${entry} (the app's entry script) did not load." >&2
            return 1
        fi
        echo "  ${site_url}: page and entry script load"
    fi

    local state
    state="$(docker inspect -f '{{.State.Running}} {{.RestartCount}}' "${SERVICE}" 2>/dev/null || echo "false ?")"
    if [ "${state}" != "true 0" ]; then
        echo "  ${SERVICE} container is not stable (running restarts: ${state})." >&2
        return 1
    fi
    return 0
}

# Most recent successful deploy whose sha differs from what is checked out now.
previous_good_sha() {
    local current
    current="$(git rev-parse HEAD)"
    [ -f "${HISTORY}" ] || return 1
    awk -v cur="${current}" '($3 == "ok" || $3 == "rolled-back") && $2 != cur { sha = $2 } END { if (sha) print sha; else exit 1 }' "${HISTORY}"
}

record() {
    printf '%s %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" >>"${HISTORY}"
}

step() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

main "$@"
exit
