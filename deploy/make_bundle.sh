#!/bin/bash
# fqp-1: the release bundle for deploy_fqp.sh (FQP_BUNDLE), made on kit-build from the reviewed commit. It holds only the commits
# after production's 7da0d29 (git bundle verify on multax needs 7da0d29, which apps/fours_customizations has).
# usage: make_bundle.sh <repo or worktree> <approved commit> <out.bundle>
set -euo pipefail
REPO=$1 SHA=$2 OUT=$3
BASELINE=7da0d2953565d19336bc0d998c79a272d8b589eb
git -C "$REPO" merge-base --is-ancestor $BASELINE "$SHA" || { echo "$SHA does not descend from $BASELINE" >&2; exit 2; }
SHA=$(git -C "$REPO" rev-parse "$SHA^{commit}")
git -C "$REPO" update-ref refs/fqp/release-bundle "$SHA"
git -C "$REPO" bundle create "$OUT" "$BASELINE..refs/fqp/release-bundle" 2>&1 | tail -1
git -C "$REPO" update-ref -d refs/fqp/release-bundle
git bundle list-heads "$OUT"
sha256sum "$OUT"
echo "FQP_SHA=$SHA FQP_BUNDLE=$OUT"
