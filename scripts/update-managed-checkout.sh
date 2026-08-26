#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 CHECKOUT" >&2
  exit 2
fi

checkout="$1"

if ! git -C "$checkout" diff --quiet || ! git -C "$checkout" diff --cached --quiet; then
  echo "managed checkout has local tracked changes: $checkout" >&2
  exit 1
fi

# Managed dependencies are disposable mirrors of the authoritative main
# branches. Release preparation may intentionally rewrite those histories, so
# a pull --ff-only is not sufficient here. Moving the local branch keeps the
# checkout reproducible without deleting untracked build caches.
git -C "$checkout" fetch --prune origin \
  +refs/heads/main:refs/remotes/origin/main
git -C "$checkout" checkout --quiet -B main origin/main
