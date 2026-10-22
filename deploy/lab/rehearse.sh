#!/bin/bash
# fqp-1 LAB ONLY: end-to-end rehearsal of deploy_fqp.sh on the private lab bench (root inside kasc-bom-lab). Every phase is the
# production script (lab copy from mklab.sh: bench path, private dirs, site names, Vox count); lab/supervisorctl stands in for
# supervisor. Timings go to <kit>/lab-results/timings.tsv.
# usage: rehearse.sh <kit dir> reset | forward | rollback | reforward
#   reset    multax stand-in fqpm.lab cloned again from labvox and migrated at production's 7da0d29, production's Item Price
#            rules + lab users (seed_ui.py), then the owner's removals simulated: the Vox Accountant rows on Sales Invoice and
#            Voided Bill are deleted (fours' own after_migrate re-adds both on every migrate: the rows the kit must remove)
#   forward  precheck, backup, code, build, migrate, reload, verify, postcheck (FQP_SHA / FQP_BUNDLE from <kit>/release.env)
#   rollback rollback, then the read-only checks again
#   reforward a new deploy stamp after the rollback: the whole forward again
set -uo pipefail
K=$1; STEP=$2
L=/home/frappe/fqp-bench
APP=fours_customizations
BASE=7da0d2953565d19336bc0d998c79a272d8b589eb
R=$K/lab-results; mkdir -p "$R"
export PATH=/root/fqp/bin:$PATH MYSQL_PWD=$(cat /root/kbom-private/db-root)
DEP="$K/deploy_fqp.lab.sh"
T=$R/timings.tsv
G="git -c safe.directory=*"
ph() {  # ph <label> <cmd...>: run, time, log; stop the rehearsal on failure unless label ends with '?'
  local lab=$1; shift; local t0 t1 rc
  echo "### $(date -u +%T) $lab: $*"
  t0=$(date +%s.%N); "$@"; rc=$?; t1=$(date +%s.%N)
  printf "%s\t%s\t%.1f\trc=%s\n" "$STEP" "$lab" "$(awk "BEGIN{print $t1 - $t0}")" "$rc" | tee -a "$T"
  [ $rc -eq 0 ] || case $lab in *\?) ;; *) echo "REHEARSAL STOPPED at $lab"; exit 1;; esac
}
asf() { su - frappe -c "cd $L && $*"; }
vox() { asf "bench --site fqpm.lab mariadb -N -e \"select count(*) from \\\`tabCustom DocPerm\\\` where role='Vox Accountant'\"" 2>/dev/null | tail -1; }
redis-cli -p 16222 ping >/dev/null 2>&1 || runuser -u frappe -- redis-server --port 16222 --bind 127.0.0.1 --save '' --appendonly no --daemonize yes --pidfile $L/config/redis.pid --dir $L/config --logfile $L/logs/redis.log
[ -f "$K/release.env" ] && . "$K/release.env"
export FQP_SHA FQP_BUNDLE

forward() {
  ph precheck "$DEP" precheck
  ph backup "$DEP" backup
  ph code "$DEP" code
  # window between code and migrate: the new code on the old schema answers the Item Price list and the page API safely
  ph "window: Guest probe after the swap, before migrate (403: an old worker imports the new module from disk)" bash -c "curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: fqpm.lab' http://127.0.0.1:18222/api/method/fours_customizations.quick_price.api.get_context"
  ph build "$DEP" build
  ph migrate "$DEP" migrate
  ph reload "$DEP" reload
  ph verify "$DEP" verify
  ph postcheck "$DEP" postcheck
  echo "Vox Accountant rows after forward: $(vox)"
}

case $STEP in
  reset)
    supervisorctl stop all >/dev/null 2>&1
    bash $K/lab/serve.sh stop >/dev/null 2>&1
    for d in $L/apps/.$APP-release-* $L/apps/.$APP-prev-* $L/apps/.$APP-failed-*; do
      [ -d "$d" ] && find "$d" -mindepth 1 -delete && rmdir "$d" && echo "removed $d"
    done
    runuser -u frappe -- $G -C $L/apps/$APP checkout -q -f --detach $BASE
    runuser -u frappe -- $G -C $L/apps/$APP clean -q -fdx
    for r in $(runuser -u frappe -- $G -C $L/apps/$APP for-each-ref --format='%(refname)' refs/fqp); do runuser -u frappe -- $G -C $L/apps/$APP update-ref -d "$r"; done
    for d in /home/frappe/fqp-release-lab /root/fqp-deploy-lab; do [ -d $d ] && find $d -mindepth 1 -delete && rmdir $d; done
    rm -f "$T"
    ph "reclone multax stand-in" bash $K/lab/reclone.sh fqpm.lab
    ph "baseline migrate at 7da0d29 (production state)" asf "bench --site fqpm.lab migrate > $L/logs/reset-migrate.log 2>&1"
    install -o frappe -g frappe -m 600 $K/lab/seed_ui.py $L/seed_ui.py
    ph "production Item Price rules + lab users" su - frappe -c "cd $L/sites && ../env/bin/python ../seed_ui.py fqpm.lab $L/lab-users.json 2>&1 | grep -v Warning | tail -2"
    ph "owner removals simulated (Vox Accountant rows on Sales Invoice, Voided Bill)" asf "bench --site fqpm.lab mariadb -e \"delete from \\\`tabCustom DocPerm\\\` where role='Vox Accountant' and parent in ('Sales Invoice', 'Voided Bill') and permlevel=0\""
    N=$(vox); echo "lab Vox Accountant baseline: $N rows"
    ph mklab bash $K/lab/mklab.sh $K "$N"
    ph "start web + worker" supervisorctl start all
    ;;
  forward) forward ;;
  rollback)
    ph rollback "$DEP" rollback
    ph "after rollback: HEAD = baseline" bash -c "[ \"\$($G -C $L/apps/$APP rev-parse HEAD)\" = $BASE ]"
    echo "Vox Accountant rows after rollback: $(vox)"
    ph "postcheck after rollback" "$DEP" postcheck
    ;;
  reforward)
    export FQP_NEW_TS=1
    ph "precheck (new stamp)" "$DEP" precheck
    unset FQP_NEW_TS
    ph backup "$DEP" backup
    ph code "$DEP" code
    ph build "$DEP" build
    ph migrate "$DEP" migrate
    ph reload "$DEP" reload
    ph verify "$DEP" verify
    ph postcheck "$DEP" postcheck
    echo "Vox Accountant rows after reforward: $(vox)"
    ;;
  phases)  # rehearse.sh <kit> phases <phase>...: continue after a fixed stop
    shift 2; for p in "$@"; do ph "$p" "$DEP" "$p"; done ;;
  *) echo "usage: rehearse.sh <kit dir> reset|forward|rollback|reforward|phases <phase>..." >&2; exit 2 ;;
esac
