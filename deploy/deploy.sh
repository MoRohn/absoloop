#!/usr/bin/env bash
# Rsync site/ to web-1 over SSH. Optionally reload the Docker Caddy container.
# Required: WEB1_HOST, WEB1_USER, WEB1_SSH_KEY
# Optional: WEB1_WEB_ROOT (default /srv/sites/absoloop.ai), WEB1_PORT (default 22),
#           WEB1_CADDY_RELOAD (1/true/yes), WEB1_CADDY_RELOAD_INPUT (same),
#           WEB1_KNOWN_HOSTS (pinned host keys; otherwise accept-new on first use).
#
# Does not read or write /srv/caddy/Caddyfile. That file is shared with other
# sites. A reload only re-reads it inside the container.

set -euo pipefail

: "${WEB1_HOST:?Set WEB1_HOST to the web-1 hostname or IP}"
: "${WEB1_USER:?Set WEB1_USER to the SSH user that can write the web root}"
: "${WEB1_SSH_KEY:?Set WEB1_SSH_KEY to the private key contents or a path to the key file}"

ROOT="${WEB1_WEB_ROOT:-/srv/sites/absoloop.ai}"
PORT="${WEB1_PORT:-22}"
ROOT="${ROOT%/}"

if [[ "$ROOT" != /* ]]; then
  echo "WEB1_WEB_ROOT must be an absolute path" >&2
  exit 1
fi

case "$ROOT" in
  /|/srv|/srv/sites|/srv/caddy|/srv/caddy/*|*Caddyfile*)
    echo "Refusing to rsync onto ${ROOT}. Use a dedicated directory such as /srv/sites/absoloop.ai. The shared Caddyfile is never overwritten." >&2
    exit 1
    ;;
esac

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SITE_DIR="${REPO_ROOT}/site"
if [[ ! -f "${SITE_DIR}/index.html" ]]; then
  echo "Missing ${SITE_DIR}/index.html" >&2
  exit 1
fi

cleanup() {
  if [[ -n "${KEY_TMP:-}" ]]; then
    rm -f "${KEY_TMP}"
  fi
  if [[ -n "${KNOWN_TMP:-}" ]]; then
    rm -f "${KNOWN_TMP}"
  fi
}
trap cleanup EXIT

if [[ -f "${WEB1_SSH_KEY}" ]]; then
  KEY_FILE="${WEB1_SSH_KEY}"
else
  KEY_TMP="$(mktemp)"
  # GitHub secrets sometimes store a PEM with literal \n sequences.
  if [[ "${WEB1_SSH_KEY}" != *$'\n'* && "${WEB1_SSH_KEY}" == *'\n'* ]]; then
    printf '%b\n' "${WEB1_SSH_KEY}" > "${KEY_TMP}"
  else
    printf '%s\n' "${WEB1_SSH_KEY}" > "${KEY_TMP}"
  fi
  chmod 600 "${KEY_TMP}"
  KEY_FILE="${KEY_TMP}"
fi

SSH_OPTS=(
  -i "${KEY_FILE}"
  -p "${PORT}"
  -o IdentitiesOnly=yes
  -o BatchMode=yes
)

if [[ -n "${WEB1_KNOWN_HOSTS:-}" ]]; then
  KNOWN_TMP="$(mktemp)"
  printf '%s\n' "${WEB1_KNOWN_HOSTS}" > "${KNOWN_TMP}"
  SSH_OPTS+=(-o "UserKnownHostsFile=${KNOWN_TMP}" -o StrictHostKeyChecking=yes)
else
  SSH_OPTS+=(-o StrictHostKeyChecking=accept-new)
fi

REMOTE="${WEB1_USER}@${WEB1_HOST}"

# shellcheck disable=SC2029
ssh "${SSH_OPTS[@]}" "${REMOTE}" "mkdir -p $(printf '%q' "${ROOT}")"

RSYNC_RSH="ssh -i $(printf '%q' "${KEY_FILE}") -p $(printf '%q' "${PORT}") -o IdentitiesOnly=yes -o BatchMode=yes"
if [[ -n "${KNOWN_TMP:-}" ]]; then
  RSYNC_RSH+=" -o UserKnownHostsFile=$(printf '%q' "${KNOWN_TMP}") -o StrictHostKeyChecking=yes"
else
  RSYNC_RSH+=" -o StrictHostKeyChecking=accept-new"
fi

rsync -az --delete \
  --chmod=D755,F644 \
  -e "${RSYNC_RSH}" \
  "${SITE_DIR}/" "${REMOTE}:${ROOT}/"

want_reload=0
case " ${WEB1_CADDY_RELOAD:-} ${WEB1_CADDY_RELOAD_INPUT:-} " in
  *\ 1\ *|*\ true\ *|*\ TRUE\ *|*\ True\ *|*\ yes\ *|*\ YES\ *) want_reload=1 ;;
esac

if [[ "${want_reload}" -eq 1 ]]; then
  # New files under /srv/sites are visible without this. Reload only after
  # someone has edited the shared Caddyfile on the host by hand.
  ssh "${SSH_OPTS[@]}" "${REMOTE}" \
    "docker exec caddy caddy reload --config /etc/caddy/Caddyfile"
fi

echo "Synced ${SITE_DIR}/ to ${REMOTE}:${ROOT}/"
