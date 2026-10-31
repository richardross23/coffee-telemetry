#!/usr/bin/env bash
# deploy-to-refuge.sh — ship this working tree to the refuge NAS, where
# the stack actually runs. Refuge has no git clone (SSH user doesn't have
# UGREEN ACL access for /volume2 under rsync's wrapper), so we tar it.
#
# What's shipped: source files only. Explicitly excluded:
#   - .git/                     (keep repo on Mac)
#   - shots/                    (runtime data, refuge is source of truth)
#   - mosquitto/data/           (runtime state)
#   - _*backup*                 (local backups from one-off migrations)
#   - .DS_Store                 (noise)
#   - logger/__pycache__        (build artifact)
#
# After the sync, container images that bake code in (shot-logger,
# native-subscriber) need `docker compose up -d --build <service>` on
# refuge to pick up changes. Web + mosquitto configs are bind-mounted so
# a `docker restart` (or no action for pure HTML) suffices.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REFUGE=${REFUGE:-refuge}
REMOTE_PATH=${REMOTE_PATH:-/volume2/docker/coffee}
DRY_RUN=${DRY_RUN:-0}

cd "$REPO_ROOT"

# Build file list — git ls-files gives us only tracked content, which is
# exactly what we want to ship (ignores backups, pycache, DS_Store, etc.
# as long as .gitignore is set up right). Fallback to a safe find if we
# ever need to ship untracked work.
FILES=$(git ls-files)

if [ -z "$FILES" ]; then
  echo "nothing to ship (git ls-files empty)" >&2
  exit 1
fi

echo "► shipping $(echo "$FILES" | wc -l | tr -d ' ') tracked files to ${REFUGE}:${REMOTE_PATH}"

if [ "$DRY_RUN" = "1" ]; then
  echo "DRY RUN — not touching remote"
  echo "$FILES"
  exit 0
fi

tar cf - $FILES | ssh "$REFUGE" "mkdir -p '$REMOTE_PATH' && tar xf - -C '$REMOTE_PATH'"
echo "► done. If you changed logger.py or native_subscriber.py, run on refuge:"
echo "    docker compose -f ${REMOTE_PATH}/compose.yml up -d --build shot-logger native-subscriber"
