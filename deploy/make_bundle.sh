#!/bin/bash
# ps-fours-2: the release bundle for deploy_fqp.sh (FQP_BUNDLE), made on kit-build from the reviewed commit. It holds only the
# commits after production's fe153b5 (git bundle verify on multax needs fe153b5, which apps/fours_customizations has).
# usage: make_bundle.sh <repo or worktree> <approved commit> <out.bundle>
set -euo pipefail
REPO=$1 SHA=$2 OUT=$3
BASELINE=fe153b5c29512a574b35c06fb1a801d7d29a704c
git -C "$REPO" merge-base --is-ancestor $BASELINE "$SHA" || { echo "$SHA does not descend from $BASELINE" >&2; exit 2; }
SHA=$(git -C "$REPO" rev-parse "$SHA^{commit}")
git -C "$REPO" update-ref refs/ps/release-bundle "$SHA"
git -C "$REPO" bundle create "$OUT" "$BASELINE..refs/ps/release-bundle" 2>&1 | tail -1
git -C "$REPO" update-ref -d refs/ps/release-bundle
git bundle list-heads "$OUT"
sha256sum "$OUT"
echo "FQP_SHA=$SHA FQP_BUNDLE=$OUT"
