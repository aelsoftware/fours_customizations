#!/bin/bash
# fqp-1 LAB ONLY (kit-build, root): copy this worktree's app files (tracked + untracked, not ignored) over the private lab
# bench's apps/fours_customizations in kasc-bom-lab, for test iterations. The lab tree then shows local changes; the deploy
# rehearsal starts from a clean checkout instead (rehearse.sh reset).
set -euo pipefail
export PATH=$PATH:/snap/bin
W=$(cd "$(dirname "$0")/../.." && pwd)
L=/home/frappe/fqp-bench
T=$(mktemp -d "$W/.fqp-tmp-XXXX")   # scratch inside the worktree (gitignored)
git -C "$W" ls-files -z --cached --others --exclude-standard | tar -C "$W" --null -T - -czf "$T/tree.tgz"
lxc file push -q "$T/tree.tgz" kasc-bom-lab/tmp/fqp-tree.tgz
lxc exec kasc-bom-lab -- bash -c "runuser -u frappe -- tar -C $L/apps/fours_customizations -xzf /tmp/fqp-tree.tgz && rm /tmp/fqp-tree.tgz"
rm -r "$T"
echo "pushed $(git -C "$W" rev-parse --short HEAD) + working tree to $L/apps/fours_customizations"
