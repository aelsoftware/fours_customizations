#!/bin/bash
# deploy_fqp.sh - ps-fours-2 price list split code release (fours_customizations feat/price-list-split, on top of the fqp-1
# Quick Price release fe153b5) on the SHARED Kit Enterprise bench on multax, site multax.kit.africa ONLY. The fqp-1 kit with a
# new baseline, release ref, file list, smoke and private directories (it never reads the fqp-1 deploy's state). The data
# step that follows the code (Company default lists, archives, page roles) is ps2.py. Runbook: RUNBOOK.md next to this file.
#
# Run as root on multax from a directory holding this file and fqp_check.py. bench and git run as the bench user (frappe)
# through 'su - frappe'. Each call runs ONE phase, prints ids, names and counts only, stops at its first failed gate (exit 2)
# and logs to /root/ps-deploy-multax/ (700). Private data (facts, backups metadata, tree tarball) go to
# /home/frappe/ps-release/<TS>/ (700/600).
#
#   precheck   read-only: sites and which have fours_customizations, code = baseline fe153b5 (clean), release commit
#              (FQP_SHA from FQP_BUNDLE or origin) descends from it, pending patches/syncs of every app on multax, Vox
#              Accountant Custom DocPerm rows = 48 (saved as the baseline), Item Price rules, site_config/kit_pos switches,
#              other-site fingerprints (jamissang, ddamu ...), disk, supervisor, jobs
#   backup     bench --site multax.kit.africa backup --with-files; the app tree (HEAD, status, tar + sha256)
#   code       release copy at FQP_SHA, checks, scheduler pause (multax), swap (one rename)               -> T_swap
#   build      bench build --app fours_customizations (other apps' assets must stay as they are)
#   migrate    bench --site multax.kit.africa migrate; Vox Accountant rows back to the 48 baseline (re-added rows removed);
#              the Quick Price page roles back to the pre-deploy ones (sync_access re-syncs them on every migrate: the
#              Phase 1 pause stays until ps2.py); other roles' permissions, site_config and kit_pos switches unchanged
#   reload     gunicorn HUP (graceful), workers group restart, clear-cache + scheduler resume (multax)    -> T_done
#   verify     read-only smoke: hooks loaded, Company default-list field, split rules hold per restricted user, consumers,
#              page lists per company, Error Log; manual list
#   postcheck  read-only: every other site (jamissang, ddamu, demo ...) not migrated and not changed
#   window     code + build + migrate + reload, one after the other (stops at the first failed gate)
#   rollback   refuses while ps2.py is applied (Company default lists set or the archives disabled: run 'ps2.py rollback'
#              first); previous tree back, migrate multax, Vox fix, the release's Company field removed, build, reload
#   status     read-only summary
#
# Never on this bench: bench restart, bench update, 'bench migrate' without --site, '--site all', migrate of any other site
# (ddamu stays in maintenance), bench build without --app fours_customizations, any permission change other than putting the
# Vox Accountant rows back to the baseline. Never prints secrets (remote URLs masked, site_config values hashed).
#
# Environment (each one an explicit lead decision, printed in the log):
#   FQP_SHA=<sha>                 REQUIRED from 'code' on (optional in precheck): the approved commit of feat/price-list-split
#   FQP_BUNDLE=/path/file.bundle  fetch the release from a git bundle (made with make_bundle.sh) instead of origin
#   FQP_REF=refs/heads/...        the ref to fetch from origin when there is no bundle (default refs/heads/feat/price-list-split)
#   FQP_ACK_SYNC=app1,app2        accept that migrate also runs/imports these apps' pending patches/files (after reading the list)
#   FQP_ACK_OTHER_FOURS=s1,s2     accept that these other sites have fours_customizations (they get the code, never a migrate)
#   FQP_ACK_PERMS=1               accept other roles' permission changes made by migrate (after reading the list)
#   FQP_NEW_TS=1                  precheck: start a new deploy stamp
#   FQP_ACK_KEEP_PS2_DATA=1       rollback: go back to fe153b5 although ps2.py is applied (amendments and mapped documents
#                                 of pre-split documents then break until 'ps2.py rollback', which also runs on fe153b5)
set -uo pipefail
umask 077

BENCH=/home/frappe/frappe-bench
SITE=multax.kit.africa                                  # the only site this kit migrates
APP=fours_customizations
BASELINE=fe153b5c29512a574b35c06fb1a801d7d29a704c       # production apps/fours_customizations on multax (fqp-1), clean (2026-10-23)
EXPECT_VOX=48                                           # Vox Accountant Custom DocPerm rows on multax (baseline = legitimate)
EXPECT_SITES="ddamu.kit.africa demo.kit.africa erp.kit.africa jamissang.efris.kit.africa jamissang.kit.africa multax.kit.africa pma-validation.local tobi.kit.africa"
DDAMU=ddamu.kit.africa                                  # stays in maintenance; never migrated
JM_SITE=jamissang.kit.africa                            # must stay unaffected (fingerprinted with every other site)
WEB_PROG=frappe-bench-web:frappe-bench-frappe-web       # program names used by the 2026-10-21 deploy on this bench
WORKERS=frappe-bench-workers                            # supervisor group: RQ workers + scheduler
PROBE=/api/method/fours_customizations.price_lists.selling_list_replacement   # Guest: 403 with the release, 417 without it
ASSET=/assets/fours_customizations/js/selling_price_list.js
RUN=/home/frappe/ps-release
KIT=$RUN/kit
LOGDIR=/root/ps-deploy-multax
PY=$BENCH/env/bin/python
HERE=$(cd "$(dirname "$0")" && pwd)
PHASE=${1:-}
TS= D= STATE= R=

stop() {
	echo "STOP: $*" >&2
	if [ -n "$STATE" ] && [ -f "$STATE/scheduler.paused" ]; then
		echo "NOTE: the scheduler of $SITE is PAUSED. 'reload' or 'rollback' resumes it; by hand: su - frappe -c 'cd $BENCH && bench --site $SITE scheduler resume'" >&2
	fi
	exit 2
}
say() { echo "== $*"; }
asf() { su - frappe -c "set -o pipefail; cd $BENCH && $*"; }               # bench user, its own umask (app trees readable by nginx)
asp() { su - frappe -c "set -o pipefail; umask 077; cd $BENCH && $*"; }    # bench user, private files (700 / 600)
chk() { asp "cd sites && $PY $KIT/fqp_check.py $*"; }                      # checks, see fqp_check.py
mask() { sed -E 's#(://)[^/@ ]+@#\1***@#g'; }
head_now() { asf "git -C apps/$APP rev-parse HEAD" 2>/dev/null; }
jget() { python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get(sys.argv[2]))" "$1" "$2"; }
running() {
	local bad
	bad=$(supervisorctl status | awk '$2 != "RUNNING" {print $1 "=" $2}' | tr '\n' ' ')
	if [ -z "$bad" ]; then echo "  supervisor: all RUNNING"; return 0; fi
	echo "  supervisor not RUNNING: $bad"; return 1
}
mark() { date -u +%FT%TZ > "$STATE/$1.ok"; echo "PASSED: $1 ($(date -u +%FT%TZ))"; }
need() { [ -f "$STATE/$1.ok" ] || stop "phase '$1' has not passed for deploy $TS: run it first"; }
need_sha() { [[ ${FQP_SHA:-} =~ ^[0-9a-f]{40}$ ]] || stop "set FQP_SHA to the approved 40-character commit of feat/price-list-split"; }

ts_load() {
	TS=$(asf "cat $RUN/DEPLOY_TS 2>/dev/null")
	[ -n "$TS" ] || stop "no deploy stamp $RUN/DEPLOY_TS: run 'precheck' first"
	[[ $TS =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || stop "malformed deploy stamp '$TS' in $RUN/DEPLOY_TS"
	D=$RUN/$TS; STATE=$LOGDIR/state/$TS; mkdir -p -m 700 "$STATE"
	R=apps/.$APP-release-$TS
}
ts_init() {
	install -d -o frappe -g frappe -m 700 "$RUN" || stop "cannot create $RUN"
	if [ "${FQP_NEW_TS:-0}" = 1 ] || ! asf "test -s $RUN/DEPLOY_TS"; then
		asp "date -u +%Y%m%dT%H%M%SZ > $RUN/DEPLOY_TS" || stop "cannot write the deploy stamp"
	elif ! asf "find $RUN/DEPLOY_TS -mmin -2160 | grep -q ."; then
		stop "deploy stamp $(asf "cat $RUN/DEPLOY_TS") is older than 36 h: set FQP_NEW_TS=1 to start a new deploy"
	fi
	ts_load
}
log_start() {
	mkdir -p -m 700 "$LOGDIR"
	LOG=$LOGDIR/$TS-$PHASE-$(date -u +%H%M%SZ).log
	exec > >(tee -a "$LOG") 2>&1
	echo "## deploy_fqp.sh $PHASE | deploy $TS | site $SITE | release ${FQP_SHA:-<FQP_SHA unset>} | $(date -u +%FT%TZ) | log $LOG"
	local v; for v in FQP_SHA FQP_BUNDLE FQP_REF FQP_ACK_SYNC FQP_ACK_OTHER_FOURS FQP_ACK_PERMS FQP_NEW_TS FQP_ACK_KEEP_PS2_DATA; do
		[ -n "${!v:-}" ] && echo "   lead decision: $v=${!v}"
	done
	return 0
}
install_kit() {
	[ -f "$HERE/fqp_check.py" ] || stop "fqp_check.py is missing next to deploy_fqp.sh"
	install -d -o frappe -g frappe -m 700 "$RUN" "$KIT" "$D" || stop "cannot create the private directories under $RUN"
	install -o frappe -g frappe -m 600 "$HERE/fqp_check.py" "$KIT/fqp_check.py" || stop "cannot install fqp_check.py into $KIT"
	echo "  helper $KIT/fqp_check.py sha256 $(sha256sum "$KIT/fqp_check.py" | cut -c1-16); private dir $D"
}

# The release commit into <repo> (apps/fours_customizations or the release copy) as refs/fqp/release, from the bundle or origin.
fetch_release() {
	local repo=$1 ref=${FQP_REF:-refs/heads/feat/price-list-split}
	if [ -n "${FQP_BUNDLE:-}" ]; then
		[ -f "$FQP_BUNDLE" ] || { echo "  FQP_BUNDLE $FQP_BUNDLE not found"; return 1; }
		install -o frappe -g frappe -m 600 "$FQP_BUNDLE" "$KIT/release.bundle" || return 1
		asf "git -C $repo bundle verify $KIT/release.bundle >/dev/null 2>&1" || { echo "  the bundle does not verify against $repo (it needs $BASELINE)"; return 1; }
		ref=$(asf "git -C $repo bundle list-heads $KIT/release.bundle" | awk '{print $2}' | head -1)
		asf "git -C $repo fetch --quiet $KIT/release.bundle '+$ref:refs/fqp/release'" 2>&1 | mask
	else
		asf "GIT_TERMINAL_PROMPT=0 timeout 120 git -C $repo fetch --quiet origin '+$ref:refs/fqp/release'" 2>&1 | mask
	fi
	local c; c=$(asf "git -C $repo rev-parse -q --verify 'refs/fqp/release^{commit}'" 2>/dev/null)
	[ "$c" = "$FQP_SHA" ] && echo "  release ref -> $c (= FQP_SHA)" && return 0
	echo "  release ref -> '${c:-none}', not FQP_SHA $FQP_SHA"; return 1
}
db_backup() {
	local tag=$1 extra=${2:-}
	asp "touch $D/.backup-start-$tag" || stop "cannot write $D"
	echo "  bench --site $SITE backup $extra"
	asp "bench --site $SITE backup $extra" > "$LOGDIR/$TS-backup-$tag.out" 2>&1 || stop "bench --site $SITE backup $extra failed: $LOGDIR/$TS-backup-$tag.out"
	asf "find sites/$SITE/private/backups -maxdepth 1 -type f -newer $D/.backup-start-$tag -exec chmod 600 {} +"
	echo "  new backup files (name, bytes; private/backups, now 600):"
	asf "find sites/$SITE/private/backups -maxdepth 1 -type f -newer $D/.backup-start-$tag -printf '    %f %s\n'" | sort
	[ -n "$(asf "find sites/$SITE/private/backups -maxdepth 1 -type f -name '*database*' -newer $D/.backup-start-$tag")" ] || stop "no new database backup file found"
}
long_trx_gate() {
	local db n
	db=$(jget "$D/facts-before.json" db_name 2>/dev/null)
	[ -n "$db" ] && [ "$db" != None ] || { echo "  WARN: no db_name in facts-before.json: long-transaction check skipped"; return 0; }
	if ! n=$(mysql -N -e "SELECT COUNT(*) FROM information_schema.innodb_trx t JOIN information_schema.processlist p ON p.id = t.trx_mysql_thread_id WHERE p.db = '$db' AND t.trx_started < NOW() - INTERVAL 60 SECOND" 2>/dev/null); then
		echo "  WARN: root mysql unavailable: long-transaction check skipped"; return 0
	fi
	echo "  transactions open > 60 s on the site's database: $n"
	[ "$n" = 0 ] || stop "an open transaction older than 60 s on the site's database would block migrate's ALTER TABLE: wait for it to finish"
}
gunicorn_arg() { ps -o args= -p "$(supervisorctl pid "$WEB_PROG")" 2>/dev/null | grep -o -E -- "$1" | head -1 | sed -E 's/^[^ =]+[ =]//'; }
web_check() {   # a Guest call of a release-only whitelisted method -> 403 'not permitted' (release) / 417 'no such method' (old code)
	local want=$1 n=${2:-40} bind codes
	bind=$(gunicorn_arg '(-b|--bind)[= ][^ ]+')
	if [ -z "$bind" ]; then echo "  web check skipped: gunicorn bind not found (use the browser check)"; return 0; fi
	if [[ $bind == unix:* ]]; then
		codes=$(for _ in $(seq 1 "$n"); do curl -s -o /dev/null -m 10 -w '%{http_code}\n' --unix-socket "${bind#unix:}" -H "Host: $SITE" -H "X-Frappe-Site-Name: $SITE" "http://localhost$PROBE"; done | sort | uniq -c | awk '{print $2 "x" $1}' | tr '\n' ' ')
	else
		codes=$(for _ in $(seq 1 "$n"); do curl -s -o /dev/null -m 10 -w '%{http_code}\n' -H "Host: $SITE" -H "X-Frappe-Site-Name: $SITE" "http://$bind$PROBE"; done | sort | uniq -c | awk '{print $2 "x" $1}' | tr '\n' ' ')
	fi
	echo "  web check: $n Guest calls to the gunicorn workers -> HTTP $codes(expect only ${want}x$n)"
	if [ "$want" = calibrate ]; then
		if [ "$codes" = "417x$n " ]; then date -u +%FT%TZ > "$STATE/webprobe.calibrated"; echo "  probe calibrated: the old code answers 417"
		else rm -f "$STATE/webprobe.calibrated"; echo "  WARN: the old code does not answer 417 here: the reload web check will not be proof (use the browser check)"; fi
		return 0
	fi
	[ "$codes" = "${want}x$n " ] || stop "a web worker does not answer as the expected code ($want): wait the graceful timeout and re-run; if it persists, a web restart (502 for EVERY site for seconds: announce it to the platform owner first)"
	return 0
}
asset_check() {
	local c
	c=$(curl -sk -o /dev/null -m 10 -w '%{http_code}' --resolve "$SITE:443:127.0.0.1" "https://$SITE$1" 2>/dev/null)
	echo "  nginx https://$SITE$1 (local) -> HTTP $c (expect $2)"
	case $c in
		"$2") ;;
		403) stop "nginx cannot read $1: check the permissions of apps/$APP (o+rx dirs, o+r files)" ;;
		*) echo "  WARN: not conclusive (HTTP $c): check the Item Price list button in the browser" ;;
	esac
}
do_reload() {   # graceful HUP of gunicorn, workers group restart, clear-cache + scheduler resume of this site. Never 'bench restart'.
	local want=$1 asset_code=$2 m old alive gt deadline
	say "gate: no background job running on the bench (a workers restart stops them after stopwaitsecs)"
	chk jobs --site $SITE --fail-any || stop "wait until the running jobs finish (or the platform owner agrees), then re-run"
	say "graceful reload of the web server (HUP)"
	supervisorctl status "$WEB_PROG" | grep -q RUNNING || stop "web program $WEB_PROG is not RUNNING"
	m=$(supervisorctl pid "$WEB_PROG"); [[ $m =~ ^[0-9]+$ ]] && [ "$m" -gt 1 ] || stop "no gunicorn master pid for $WEB_PROG"
	old=$(pgrep -P "$m" | sort -n | tr '\n' ' ')
	gt=$(gunicorn_arg '--graceful-timeout[= ][0-9]+'); gt=${gt:-30}
	echo "  gunicorn master $m; workers before: $(echo "$old" | wc -w); graceful timeout ${gt} s; --preload: $(ps -o args= -p "$m" | grep -c -- '--preload')"
	supervisorctl signal HUP "$WEB_PROG" | tail -1
	deadline=$(( $(date +%s) + gt + 20 ))
	while :; do
		alive=0
		for p in $old; do [ "$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')" = "$m" ] && alive=$((alive + 1)); done
		[ "$alive" -eq 0 ] && break
		[ "$(date +%s)" -ge "$deadline" ] && stop "$alive old web workers still alive after $((gt + 20)) s: re-run this phase"
		sleep 2
	done
	echo "  old workers exited; workers now: $(pgrep -P "$m" | wc -l) (all new); master pid $(supervisorctl pid "$WEB_PROG")"
	say "restart the workers group (warm shutdown: running jobs finish, queued jobs stay in Redis)"
	supervisorctl status | grep "^$WORKERS:" | awk '{print "  " $1, $2}'
	supervisorctl restart "$WORKERS:" | sed 's/^/  /'
	for _ in $(seq 1 30); do running >/dev/null && break; sleep 2; done
	running || stop "not all supervisor programs RUNNING after the workers restart"
	say "clear this site's cache and resume its scheduler"
	asf "bench --site $SITE clear-cache" || stop "clear-cache failed"
	asf "bench --site $SITE scheduler resume" || stop "scheduler resume failed"
	rm -f "$STATE/scheduler.paused"
	date -u +%FT%TZ > "$STATE/T_done"; echo "  T_done $(cat "$STATE/T_done")"
	web_check "$want" 40
	asset_check "$ASSET" "$asset_code"
}
vox_fix() {   # Vox Accountant rows back to the precheck baseline (rows re-added by migrate removed, changed rows restored)
	say "Vox Accountant Custom DocPerm rows: still $EXPECT_VOX (the baseline); rows a migrate re-added are removed"
	chk vox-fix --site $SITE --baseline $D/facts-before.json --expect $EXPECT_VOX --apply || stop "Vox Accountant rows: see above (the baseline file is $D/facts-before.json)"
}
page_fix() {  # fours' after_migrate (sync_access) re-syncs the Quick Price page roles: keep the pre-deploy ones (Phase 1 pause)
	say "Quick Price page roles as before the deploy (ps2.py resumes the page; a rollback keeps it paused)"
	chk page-roles --site $SITE --baseline $D/facts-before.json --apply || stop "page roles: see above"
}

# ------------------------------------------------------------------------------------------------------------------------------
[ "$(id -u)" -eq 0 ] || { echo "run as root on multax" >&2; exit 2; }
[ -d "$BENCH/apps/$APP" ] && [ -f "$BENCH/sites/$SITE/site_config.json" ] || { echo "STOP: $BENCH / $SITE not found: not the multax bench" >&2; exit 2; }
case "$PHASE" in
	precheck) ts_init ;;
	backup|code|build|migrate|reload|verify|postcheck|rollback|status) ts_load ;;
	window) ts_load; for p in code build migrate reload; do bash "$HERE/$(basename "$0")" "$p" || exit $?; done; exit 0 ;;
	*) sed -n '2,40p' "$0"; exit 2 ;;
esac
log_start
install_kit

case "$PHASE" in
# ------------------------------------------------------------------------------------------------------------------------------
precheck)
	H=$(head_now); echo "  apps/$APP HEAD $H"
	say "sites of the bench, and which have $APP (read from each site's database; nothing is changed)"
	have=$(asf "ls -d sites/*/site_config.json" | sed 's#sites/##; s#/site_config.json##' | sort | xargs)
	echo "  sites: $have"
	[ "$have" = "$(echo $EXPECT_SITES | tr ' ' '\n' | sort | xargs)" ] || echo "  WARN: the site list differs from the 2026-10-22 list ($EXPECT_SITES): read it"
	chk sites --out $D/sites.json || stop "a site could not be read"
	others=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(' '.join(s for s, r in sorted(d.items()) if '$APP' in r.get('installed_apps', []) and s != '$SITE'))" "$D/sites.json")
	python3 -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if '$APP' in d['$SITE'].get('installed_apps', []) else 1)" "$D/sites.json" || stop "$APP is not installed on $SITE"
	if [ -n "$others" ]; then
		echo "  other sites with $APP: $others (they would run the new code WITHOUT a migrate: this kit migrates $SITE only)"
		[ "$(echo "$others" | tr ' ' ',')" = "${FQP_ACK_OTHER_FOURS:-}" ] || stop "other sites have $APP: the lead decides (FQP_ACK_OTHER_FOURS=$(echo "$others" | tr ' ' ','))"
	else
		echo "  only $SITE has $APP: the kit touches only $SITE"
	fi
	python3 -c "import json,sys; d=json.load(open(sys.argv[1])); r=d.get('$DDAMU'); sys.exit(0 if r is None or r.get('maintenance_mode') == 1 else 1)" "$D/sites.json" || echo "  WARN: $DDAMU is not in maintenance mode today"

	say "production code = baseline $BASELINE, clean"
	[ "$H" = "$BASELINE" ] || { [ -n "${FQP_SHA:-}" ] && [ "$H" = "$FQP_SHA" ] && echo "  the release is already in place"; } || stop "apps/$APP HEAD is $H, not the baseline $BASELINE: reconcile first"
	st=$(asf "git -C apps/$APP status --porcelain" | grep -v -E '__pycache__|\.pyc$' | wc -l)
	[ "$st" = 0 ] && echo "  git status: clean" || { asf "git -C apps/$APP status --porcelain" | head -10 | sed 's/^/  /'; stop "apps/$APP has $st local changes: production code changed after 2026-10-22 - reconcile first"; }
	asf "git -C apps/$APP remote -v" | mask | sed 's/^/  remote: /'

	if [ -n "${FQP_SHA:-}" ] && [ "$H" = "$BASELINE" ]; then
		need_sha
		say "the release commit (read-only preview in a private copy)"
		asp "rm -rf $D/preview && git clone --quiet --no-checkout apps/$APP $D/preview" || stop "cannot clone for the preview"
		fetch_release $D/preview || stop "the release commit is not available (FQP_BUNDLE=... or FQP_REF on origin)"
		asf "git -C $D/preview merge-base --is-ancestor $BASELINE $FQP_SHA" || stop "the release does not descend from the baseline $BASELINE"
		asf "git -C $D/preview diff --stat $BASELINE $FQP_SHA | tail -1" | sed 's/^/  change: /'
		asf "git -C $D/preview diff --name-only $BASELINE $FQP_SHA -- $APP/patches.txt pyproject.toml requirements.txt package.json" | grep -q . && stop "the release changes patches.txt / dependencies (this kit expects none)"
		asp "git -C $D/preview checkout --quiet --detach $FQP_SHA" || stop "checkout of the preview failed"
		chk pending --site $SITE --root $APP=$D/preview/$APP --allow-app $APP --no-gate --out $D/pending-release-preview.json || true
	fi

	say "pending patches and files migrate would import, every app of $SITE (current code)"
	chk pending --site $SITE --allow-app "$APP,${FQP_ACK_SYNC:-}" --out $D/pending-precheck.json || stop "migrate would do more than this release: read the list above"

	say "facts of $SITE (Vox Accountant baseline, permissions, switches) -> $D/facts-precheck.json"
	if [ "$H" = "$BASELINE" ]; then chk facts --site $SITE --out $D/facts-precheck.json --expect-vox $EXPECT_VOX --expect pre || stop "facts"
	else chk facts --site $SITE --out $D/facts-precheck.json || stop "facts"; fi

	say "environment"
	asf "./env/bin/python --version" | sed 's/^/  /'
	asf "command -v node >/dev/null && node --version" | sed 's/^/  node /' || stop "the bench user has no node on its PATH: 'bench build --app $APP' would fail"
	p=$(asf "./env/bin/python -c 'import $APP, os; print(os.path.dirname($APP.__file__))'" 2>/dev/null | tail -1)
	echo "  $APP imports from $p"
	[ "$p" = "$BENCH/apps/$APP/$APP" ] || stop "$APP must import from $BENCH/apps/$APP/$APP"
	[ "$(readlink "$BENCH/sites/assets/$APP")" = "$BENCH/apps/$APP/$APP/public" ] || stop "sites/assets/$APP does not point at $BENCH/apps/$APP/$APP/public"
	df -h "$BENCH/apps" "$BENCH/sites" | sed 's/^/  /'
	if mv --help | grep -q -- '--exchange'; then
		asp "rm -rf $D/xa $D/xb && mkdir $D/xa $D/xb && touch $D/xa/probe && mv --exchange -T $D/xa $D/xb && test -e $D/xb/probe && rm -rf $D/xa $D/xb" \
			&& echo "  mv --exchange -T works" || stop "mv --exchange -T fails on this filesystem: settle the swap method first"
	else
		echo "  WARN: no mv --exchange: the swap uses two renames (apps/$APP missing for a few ms)"
	fi
	supervisorctl status | awk '{print "  " $1, $2}'
	running || stop "a supervisor program is not RUNNING"
	supervisorctl status | grep -q "^$WORKERS:" || stop "workers group $WORKERS not found"
	echo "  gunicorn: bind $(gunicorn_arg '(-b|--bind)[= ][^ ]+'), workers $(gunicorn_arg '(-w|--workers)[= ][0-9]+'), graceful-timeout $(gunicorn_arg '--graceful-timeout[= ][0-9]+'), preload $(ps -o args= -p "$(supervisorctl pid "$WEB_PROG")" | grep -c -- '--preload')"
	[ "$H" = "$BASELINE" ] && web_check calibrate 10
	free=$(df -B1 --output=avail "$BENCH" | tail -1 | tr -d ' ')
	app=$(du -sb "$BENCH/apps/$APP" | cut -f1); sited=$(du -sb "$BENCH/sites/$SITE" | cut -f1)
	dbmb=$(jget "$D/facts-precheck.json" db_size_mb); dbmb=${dbmb//None/0}
	needb=$(( 2 * app + sited + dbmb * 1048576 + 1073741824 ))
	echo "  free $((free / 1048576)) MB; need about $((needb / 1048576)) MB"
	[ "$free" -ge "$needb" ] || stop "not enough free disk for the release copy and the backups"
	chk jobs --site $SITE --site-only || true

	fp=$D/fp-before.json
	[ -f "$STATE/code.ok" ] && fp=$D/fp-precheck-$(date -u +%H%M%SZ).json
	say "other sites ($JM_SITE, $DDAMU, ...): fingerprint (read-only) -> $fp"
	chk fingerprint --exclude $SITE --out $fp || stop "fingerprint of the other sites failed"
	mark precheck
	;;
# ------------------------------------------------------------------------------------------------------------------------------
backup)
	need precheck
	H=$(head_now); [ "$H" = "$BASELINE" ] || stop "apps/$APP is not the baseline: backup belongs before the code release"
	say "full site backup with files - $SITE only"
	db_backup "full" "--with-files"
	say "the current app tree, its HEAD and local state -> $D"
	asp "git -C apps/$APP rev-parse HEAD > $D/app-HEAD.txt" \
		&& asp "git -C apps/$APP status --porcelain > $D/app-status.txt" \
		&& asp "tar -C apps -czf $D/app-tree.tar.gz $APP" \
		&& asp "sha256sum $D/app-tree.tar.gz > $D/app-tree.sha256" || stop "tree backup failed"
	echo "  HEAD $(asf "cat $D/app-HEAD.txt"); status lines $(asf "grep -c . $D/app-status.txt")"
	echo "  tree tarball $(asf "stat -c %s $D/app-tree.tar.gz") bytes, sha256 $(asf "cut -c1-16 $D/app-tree.sha256")"
	asf "sha256sum -c --quiet $D/app-tree.sha256" || stop "tarball checksum does not verify"
	say "facts right before the code release -> $D/facts-before.json (the Vox Accountant baseline of the migrate)"
	chk facts --site $SITE --out $D/facts-before.json --expect-vox $EXPECT_VOX --expect pre || stop "facts before the code release"
	echo "  To confirm: copy the newest backup files off the host with the platform's usual off-site job (not done by this kit)"
	mark backup
	;;
# ------------------------------------------------------------------------------------------------------------------------------
code)
	need backup
	need_sha
	H=$(head_now); [ "$H" != "$FQP_SHA" ] || stop "the release is already in place"
	[ "$H" = "$(asf "cat $D/app-HEAD.txt")" ] || stop "apps/$APP HEAD changed since the backup: re-run backup"
	st=$(asf "git -C apps/$APP status --porcelain" | grep -v -E '__pycache__|\.pyc$' | wc -l)
	[ "$st" = 0 ] || stop "apps/$APP has local changes since the precheck"
	chk jobs --site $SITE --site-only --fail-any || stop "a background job of $SITE is running"
	long_trx_gate
	chk pending --site $SITE --allow-app "$APP,${FQP_ACK_SYNC:-}" --out $D/pending-code.json || stop "something is pending with the current code"

	say "release copy $R"
	asf "test -e apps/.$APP-prev-$TS" && stop "apps/.$APP-prev-$TS exists already: inspect it before a new swap"
	if asf "test -e $R"; then echo "  $R exists: re-checked below"
	else asf "umask 022; git clone --quiet apps/$APP $R" || stop "git clone of the release copy failed"; fi
	asf "u=\$(git -C apps/$APP remote get-url origin 2>/dev/null) && git -C $R remote set-url origin \"\$u\"" || echo "  note: apps/$APP has no origin remote"
	fetch_release $R || stop "the release commit is not available"
	asf "umask 022; git -C $R checkout --quiet --detach $FQP_SHA" || stop "checkout of $FQP_SHA in the release copy failed"

	say "check the release copy"
	[ "$(asf "git -C $R rev-parse HEAD")" = "$FQP_SHA" ] && echo "  HEAD ok" || stop "release copy HEAD is not $FQP_SHA"
	[ -z "$(asf "git -C $R status --porcelain")" ] && echo "  tree clean" || stop "release copy is not clean"
	asf "git -C $R merge-base --is-ancestor $BASELINE HEAD" && echo "  contains the baseline" || stop "release does not contain the baseline"
	chk syntax --dir $BENCH/$R/$APP | sed 's/^/  /'; [ "${PIPESTATUS[0]}" -eq 0 ] || stop "Python syntax errors in the release"
	chk jsoncheck --dir $BENCH/$R/$APP | sed 's/^/  /'; [ "${PIPESTATUS[0]}" -eq 0 ] || stop "bad JSON in the release"
	asf "git -C $R diff --stat $BASELINE HEAD | tail -1" | sed 's/^/  size of the change: /'
	asf "git -C $R diff --name-only $BASELINE HEAD -- $APP/patches.txt pyproject.toml requirements.txt package.json" | grep -q . && stop "the release changes patches.txt / dependencies"
	asf "grep -q 'fours_customizations.price_lists.item_price_has_permission' $R/$APP/hooks.py" && echo "  hooks.py wires price_lists" || stop "the release's hooks.py does not wire price_lists"
	for f in $APP/price_lists.py $APP/public/js/selling_price_list.js $APP/quick_price/api.py $APP/landed_cost_handler.py $APP/tests/test_price_lists.py; do
		asf "test -f $R/$f" || stop "release lacks $f"
	done
	nfr=$(find "$BENCH/$R" ! -user frappe | wc -l); [ "$nfr" -eq 0 ] || stop "$nfr paths in the release copy are not owned by frappe"
	bad=$(find "$BENCH/$R/$APP/public" \( -type f ! -perm -o=r \) -o \( -type d ! -perm -o=x \) | wc -l)
	[ "$bad" -eq 0 ] && echo "  release public files readable by nginx" || stop "$bad release files/dirs are not world-readable: remove $R and re-run 'code'"
	chk pending --site $SITE --root $APP=$BENCH/$R/$APP --allow-app $APP --no-gate --out $D/pending-release.json || true

	say "pause the scheduler of $SITE"
	asf "bench --site $SITE scheduler pause" || stop "scheduler pause failed"
	date -u +%FT%TZ > "$STATE/scheduler.paused"
	say "swap (one rename)"
	date -u +%FT%TZ > "$STATE/T_swap"; echo "  T_swap $(cat "$STATE/T_swap")"
	asf "if mv --help | grep -q -- '--exchange'; then mv --exchange -T $R apps/$APP && mv -T $R apps/.$APP-prev-$TS; else mv -T apps/$APP apps/.$APP-prev-$TS && mv -T $R apps/$APP; fi" \
		|| stop "SWAP FAILED: look at apps/$APP, $R and apps/.$APP-prev-$TS now; 'rollback' restores the previous tree"
	asp "echo $TS > $RUN/LAST_TS"
	[ "$(head_now)" = "$FQP_SHA" ] || stop "after the swap apps/$APP HEAD is not $FQP_SHA: run rollback"
	echo "  apps/$APP HEAD = $FQP_SHA; previous tree in apps/.$APP-prev-$TS"
	mark code
	echo "NEXT: build, then migrate - now"
	;;
# ------------------------------------------------------------------------------------------------------------------------------
build)
	need code
	say "bench build --app $APP (other apps' assets must stay as they are)"
	chk assets --out $D/assets-before.json || stop "assets snapshot"
	t0=$(date +%s)
	asf "bench build --app $APP" > "$LOGDIR/$TS-build.out" 2>&1; rc=$?
	echo "  rc $rc ($(( $(date +%s) - t0 )) s); output $LOGDIR/$TS-build.out"
	tr '\r' '\n' < "$LOGDIR/$TS-build.out" | grep -v -E '^\s*$' | tail -6 | sed 's/^/  | /'
	[ "$rc" -eq 0 ] || stop "bench build --app $APP failed"
	chk assets --out $D/assets-after.json || stop "assets snapshot"
	chk assets-compare --before $D/assets-before.json --after $D/assets-after.json || stop "the build changed other apps' assets"
	[ "$(readlink "$BENCH/sites/assets/$APP")" = "$BENCH/apps/$APP/$APP/public" ] || stop "sites/assets/$APP does not point at the app's public folder"
	mark build
	;;
# ------------------------------------------------------------------------------------------------------------------------------
migrate)
	need build
	[ "$(head_now)" = "${FQP_SHA:-x}" ] || stop "apps/$APP is not the release (FQP_SHA)"
	say "bench --site $SITE migrate ($SITE ONLY)"
	t0=$(date +%s); echo "  start $(date -u +%FT%TZ)"
	asf "bench --site $SITE migrate" > "$LOGDIR/$TS-migrate.out" 2>&1; rc=$?
	echo "  end $(date -u +%FT%TZ) rc $rc ($(( $(date +%s) - t0 )) s); output $LOGDIR/$TS-migrate.out"
	tr '\r' '\n' < "$LOGDIR/$TS-migrate.out" | grep -v -E '^\s*$|Updating DocTypes|^Deleting|^Removing orphan' | tail -15 | sed 's/^/  | /'
	[ "$rc" -eq 0 ] || stop "migrate failed (rc $rc): re-run 'migrate' (idempotent) or 'rollback'"
	vox_fix
	page_fix
	say "every other permission, site_config and the kit_pos switches as before"
	chk facts --site $SITE --out $D/facts-after-migrate.json || stop "facts after migrate"
	if ! chk perms-compare --before $D/facts-before.json --after $D/facts-after-migrate.json; then
		[ "${FQP_ACK_PERMS:-0}" = 1 ] && echo "  FQP_ACK_PERMS=1: accepted by the lead" || stop "other roles' permissions changed (list above)"
	fi
	chk switches-compare --before $D/facts-before.json --after $D/facts-after-migrate.json || stop "site_config / kit_pos switches changed"
	mark migrate
	;;
# ------------------------------------------------------------------------------------------------------------------------------
reload)
	need migrate
	do_reload 403 200
	mark reload
	echo "NEXT: verify"
	;;
# ------------------------------------------------------------------------------------------------------------------------------
verify)
	need reload
	[ "$(head_now)" = "${FQP_SHA:-x}" ] || stop "apps/$APP is not the release (FQP_SHA)"
	[ -z "$(asf "git -C apps/$APP status --porcelain --untracked-files=no")" ] || stop "tracked files changed in apps/$APP"
	running || stop "a supervisor program is not RUNNING"
	say "smoke (read-only)"
	chk smoke --site $SITE --since "$(cat "$STATE/T_swap")" || stop "smoke failed: decide 'rollback' with the platform owner"
	chk vox-fix --site $SITE --baseline $D/facts-before.json --expect $EXPECT_VOX || stop "Vox Accountant rows differ from the baseline"
	web_check 403 20
	asset_check "$ASSET" 200
	cat <<-EOF
	== manual checks (browser) - tick each in the change log:
	   1  as the Makindye Vox Accountant: Item Price list shows only Makindye-list rows, Standard Selling rows of bar items
	      and Standard Buying rows of bar items (no 4S item, no Nansana-list row)
	   2  as a 4S Accountant: Item Price list shows only 4S-list rows and 4S items on lists without a company
	   3  as a Vox Accountant: open Price List 'Standard Selling', tick Enabled off, save -> refused ('Only an administrator')
	   4  a new Sales Invoice (Vox Lounge) from Desk: the price list is the company's own list after ps2.py; amend one
	      pre-split invoice: the list shows the company's list, line rates unchanged, qty change re-prices only that row
	   5  hard refresh; open Item Price, Sales Invoice, Quotation and the Kit POS desk pages: no JS errors in the console
	   6  the data step follows: ps2.py plan / apply / verify (RUNBOOK.md); the Quick Price page stays paused until then
	Smoke result (automated checks PASS at $(date -u +%FT%TZ)); T_swap $(cat "$STATE/T_swap"), T_done $(cat "$STATE/T_done")
	EOF
	mark verify
	echo "NEXT: postcheck"
	;;
# ------------------------------------------------------------------------------------------------------------------------------
postcheck)
	asf "test -s $D/fp-before.json" || stop "no before-fingerprint for deploy $TS (precheck takes it)"
	say "other sites must not have been migrated or changed (read-only)"
	now=$(date -u +%H%M%SZ)
	chk fingerprint --exclude $SITE --out $D/fp-after-$now.json || stop "fingerprint failed"
	chk fp-compare --before $D/fp-before.json --after $D/fp-after-$now.json || stop "another site changed during the deploy window"
	python3 -c "import json,sys; d=json.load(open(sys.argv[1]))['sites'].get('$DDAMU'); sys.exit(0 if d is None or d.get('maintenance_mode') == 1 else 1)" "$D/fp-after-$now.json" \
		|| stop "$DDAMU is not in maintenance mode"
	echo "  $DDAMU still in maintenance; $JM_SITE unchanged (in the comparison above)"
	mark postcheck
	;;
# ------------------------------------------------------------------------------------------------------------------------------
rollback)
	TSL=$(asf "cat $RUN/LAST_TS 2>/dev/null")
	[ -n "$TSL" ] || stop "no $RUN/LAST_TS: the swap never happened, nothing to roll back (if 'code' paused the scheduler: bench --site $SITE scheduler resume)"
	[[ $TSL =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || stop "malformed LAST_TS '$TSL'"
	DL=$RUN/$TSL; SAVED=$(asf "cat $DL/app-HEAD.txt 2>/dev/null"); H=$(head_now)
	echo "  swap of $TSL; HEAD now $H; pre-release HEAD $SAVED; apps/.$APP-prev-$TSL $(asf "test -d apps/.$APP-prev-$TSL && echo present || echo MISSING")"
	[ -n "$SAVED" ] || stop "no $DL/app-HEAD.txt (backup of that swap)"
	asf "test -s $DL/facts-before.json" || stop "no $DL/facts-before.json (the Vox Accountant baseline)"
	say "the Phase 2 data step must be rolled back first (read-only)"
	if ! chk ps2-state --site $SITE; then
		[ "${FQP_ACK_KEEP_PS2_DATA:-}" = 1 ] || stop "ps2.py is applied: run it with PS_MODE=rollback (same PS_TS) first, then this rollback (FQP_ACK_KEEP_PS2_DATA=1 overrides: a lead decision)"
		echo "  FQP_ACK_KEEP_PS2_DATA=1: going back with the ps2 data in place (lead decision); then run ps2.py rollback and rollback-cleanup --apply"
	fi
	if [ "$H" != "$SAVED" ]; then
		say "swap back (the failed release is kept for analysis)"
		asf "test -e apps/.$APP-failed-$TSL" && stop "apps/.$APP-failed-$TSL exists already: move it away first"
		asf "bench --site $SITE scheduler pause" || stop "scheduler pause failed"
		date -u +%FT%TZ > "$STATE/scheduler.paused"
		if ! asf "test -d apps/.$APP-prev-$TSL"; then
			asf "sha256sum -c --quiet $DL/app-tree.sha256" || stop "MISSING apps/.$APP-prev-$TSL and the tarball does not verify"
			asf "rm -rf apps/.$APP-restore-$TSL && mkdir apps/.$APP-restore-$TSL && umask 022 && tar -xpzf $DL/app-tree.tar.gz -C apps/.$APP-restore-$TSL && mv apps/.$APP-restore-$TSL/$APP apps/.$APP-prev-$TSL && rmdir apps/.$APP-restore-$TSL" \
				|| stop "restore of the tree tarball failed"
			echo "  apps/.$APP-prev-$TSL rebuilt from the tree tarball"
		fi
		asf "if mv --help | grep -q -- '--exchange'; then mv --exchange -T apps/.$APP-prev-$TSL apps/$APP && mv -T apps/.$APP-prev-$TSL apps/.$APP-failed-$TSL; else mv -T apps/$APP apps/.$APP-failed-$TSL && mv -T apps/.$APP-prev-$TSL apps/$APP; fi" \
			|| stop "SWAP BACK FAILED: look at apps/$APP, apps/.$APP-prev-$TSL and apps/.$APP-failed-$TSL now"
		[ "$(head_now)" = "$SAVED" ] || stop "after the swap back HEAD is not the pre-release $SAVED"
		echo "  apps/$APP HEAD = $SAVED; failed release kept in apps/.$APP-failed-$TSL"
	else
		echo "  the code is already the pre-release tree"
		asf "bench --site $SITE scheduler pause" || stop "scheduler pause failed"; date -u +%FT%TZ > "$STATE/scheduler.paused"
	fi
	say "bench --site $SITE migrate ($SITE only), Vox fix, release records, build, reload"
	date -u +%FT%TZ > "$STATE/T_rollback"
	asf "bench --site $SITE migrate" > "$LOGDIR/$TS-rollback-migrate.out" 2>&1 || stop "rollback migrate failed: $LOGDIR/$TS-rollback-migrate.out"
	tr '\r' '\n' < "$LOGDIR/$TS-rollback-migrate.out" | grep -v -E '^\s*$|Updating DocTypes' | tail -8 | sed 's/^/  | /'
	D=$DL vox_fix
	D=$DL page_fix
	chk rollback-cleanup --site $SITE --apply || stop "release records cleanup failed"
	chk smoke --site $SITE --pre || stop "release objects still present after the rollback"
	asf "bench build --app $APP" > "$LOGDIR/$TS-rollback-build.out" 2>&1 || stop "rollback build failed: $LOGDIR/$TS-rollback-build.out"
	do_reload 417 404
	echo "  what stays: the Phase 1 data (company lists, prices, consumers); prices changed meanwhile keep their new rates"
	echo "  (Versions show the old ones). Last resort for data: the backup of $TSL - a platform-owner decision."
	mark rollback
	;;
# ------------------------------------------------------------------------------------------------------------------------------
status)
	echo "  deploy $TS; LAST_TS $(asf "cat $RUN/LAST_TS 2>/dev/null" || echo none); apps/$APP HEAD $(head_now)"
	echo "  phases passed: $(cd "$STATE" && ls -- *.ok 2>/dev/null | sed 's/\.ok$//' | tr '\n' ' ')"
	echo "  T_swap $(cat "$STATE/T_swap" 2>/dev/null || echo -), T_done $(cat "$STATE/T_done" 2>/dev/null || echo -), scheduler paused by this kit: $([ -f "$STATE/scheduler.paused" ] && echo yes || echo no)"
	echo "  apps/ entries: $(asf "ls -d apps/.$APP-* 2>/dev/null | tr '\n' ' '")"
	running || true
	;;
esac
