#!/bin/bash
# fqp-1 LAB ONLY: a PRIVATE bench inside kasc-bom-lab (root INSIDE the container) to test Quick Price Update and rehearse
# deploy_fqp.sh. Modelled on EB-DEPLOY-B173's lab. The shared bench (/home/frappe/frappe-bench and its sites) is never changed:
#   env      venv whose .pth puts OUR apps/frappe + apps/fours_customizations first, then the shared env's site-packages
#   apps     frappe = git clone --shared of the shared frappe (same commit + same local diff); fours_customizations = clone of
#            the LAB upstream /home/frappe/fqp-upstream.git (stands in for GitHub; production's 7da0d29 + the feature branch),
#            checked out at production's 7da0d29; every other app a symlink to the shared tree (read only use)
#   sites    fqpm.lab (labvox clone = multax stand-in), fqpd.lab (labdemo clone = demo stand-in), fqpdd.lab (labddamu clone =
#            ddamu stand-in, maintenance mode on, NEVER migrated); own databases/users _fqp*; scheduler paused; mails muted
#   assets   a copy of the shared sites/assets (18 MB) whose fours_customizations link points at OUR app
#   services own redis :16222; gunicorn :18222 (2 workers, --preload as production) and one RQ worker via lab/supervisorctl
# usage: setup.sh <upstream.bundle>     (teardown: teardown.sh)
set -euo pipefail
BUNDLE=$1
L=/home/frappe/fqp-bench
UP=/home/frappe/fqp-upstream.git
S=/home/frappe/frappe-bench
BASE=7da0d29
PY=$(readlink -f $S/env/bin/python)
SP=$S/env/lib/python3.14/site-packages
test ! -e $L && test ! -e $UP || { echo "lab exists: run teardown.sh first" >&2; exit 2; }
export MYSQL_PWD=$(cat /root/kbom-private/db-root)
G="git -c safe.directory=*"

runuser -u frappe -- $G init -q --bare $UP
runuser -u frappe -- cp "$BUNDLE" /tmp/fqp-upstream.bundle 2>/dev/null || { cp "$BUNDLE" /tmp/fqp-upstream.bundle; chown frappe:frappe /tmp/fqp-upstream.bundle; }
runuser -u frappe -- $G -C $UP fetch -q /tmp/fqp-upstream.bundle 'refs/heads/*:refs/heads/*'
rm -f /tmp/fqp-upstream.bundle

mkdir -p $L/apps $L/sites $L/config/pids $L/logs && chown -R frappe:frappe $L
runuser -u frappe -- $G clone -q --no-checkout $UP $L/apps/fours_customizations
runuser -u frappe -- $G -C $L/apps/fours_customizations checkout -q --detach $BASE
runuser -u frappe -- $G clone -q --shared --no-checkout $S/apps/frappe $L/apps/frappe
runuser -u frappe -- $G -C $L/apps/frappe checkout -q --detach "$($G -C $S/apps/frappe rev-parse HEAD)"
$G -C $S/apps/frappe diff | runuser -u frappe -- $G -C $L/apps/frappe apply
ln -s $S/apps/frappe/node_modules $L/apps/frappe/node_modules
# frappe's built bundles and node_modules link are ignored by git: use the shared ones (same commit + diff; read only)
ln -s $S/apps/frappe/frappe/public/dist $L/apps/frappe/frappe/public/dist
ln -s $S/apps/frappe/node_modules $L/apps/frappe/frappe/public/node_modules
for a in $(cat $S/sites/apps.txt); do case $a in frappe|fours_customizations) ;; *) ln -s $S/apps/$a $L/apps/$a;; esac; done

runuser -u frappe -- $PY -m venv --without-pip $L/env
printf '%s\n' $L/apps/frappe $L/apps/fours_customizations > $L/env/lib/python3.14/site-packages/00-fqp.pth
echo "import site; site.addsitedir('$SP')" > $L/env/lib/python3.14/site-packages/zz-shared.pth
chown frappe:frappe $L/env/lib/python3.14/site-packages/*.pth
runuser -u frappe -- $L/env/bin/python -c "import frappe, fours_customizations, erpnext; print('imports:', frappe.__file__, fours_customizations.__file__, erpnext.__file__)"

cp $S/sites/apps.txt $L/sites/apps.txt
cp -a $S/sites/assets $L/sites/assets
rm $L/sites/assets/fours_customizations $L/sites/assets/frappe
ln -s $L/apps/fours_customizations/fours_customizations/public $L/sites/assets/fours_customizations
ln -s $L/apps/frappe/frappe/public $L/sites/assets/frappe
python3 - $L/sites/common_site_config.json <<'PY'
import json, sys
json.dump({"background_workers": 1, "frappe_user": "frappe", "gunicorn_workers": 2, "live_reload": False, "redis_cache": "redis://127.0.0.1:16222",
	"redis_queue": "redis://127.0.0.1:16222", "redis_socketio": "redis://127.0.0.1:16222", "restart_supervisor_on_update": False,
	"restart_systemd_on_update": False, "serve_default_site": False, "server_script_enabled": 1, "socketio_port": 19222,
	"use_redis_auth": False, "webserver_port": 18222}, open(sys.argv[1], "w"), indent=1)
PY
chown -R frappe:frappe $L/sites

clone() {  # clone <source site> <lab site> [maintenance]
  local SRC DB PW
  SRC=$(python3 -c "import json;print(json.load(open('$S/sites/$1/site_config.json'))['db_name'])")
  DB=_fqp$(openssl rand -hex 6); PW=$(openssl rand -hex 16)
  mariadb -uroot -e "CREATE DATABASE \`$DB\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; CREATE USER '$DB'@'localhost' IDENTIFIED BY '$PW'; CREATE USER '$DB'@'%' IDENTIFIED BY '$PW'; GRANT ALL PRIVILEGES ON \`$DB\`.* TO '$DB'@'localhost'; GRANT ALL PRIVILEGES ON \`$DB\`.* TO '$DB'@'%';"
  mariadb-dump -uroot --single-transaction --routines --triggers "$SRC" | mariadb -uroot "$DB"
  mkdir -p $L/sites/$2/{private/backups,private/files,public/files,logs,locks}
  python3 - "$S/sites/$1/site_config.json" "$L/sites/$2/site_config.json" "$DB" "$PW" "${3:-0}" <<'PY'
import json, sys
src = json.load(open(sys.argv[1]))
c = {k: v for k, v in src.items() if not k.startswith("saleslive")}
c.update(db_name=sys.argv[3], db_user=sys.argv[3], db_password=sys.argv[4], pause_scheduler=1, mute_emails=1, developer_mode=0)
if sys.argv[5] == "1":
	c["maintenance_mode"] = 1
json.dump(c, open(sys.argv[2], "w"), indent=1)
PY
  chown -R frappe:frappe $L/sites/$2; chmod 600 $L/sites/$2/site_config.json
  echo "cloned $1 -> $2"
}
clone labvox.kit.africa fqpm.lab
clone labdemo.kit.africa fqpd.lab
clone labddamu.kit.africa fqpdd.lab 1

runuser -u frappe -- redis-server --port 16222 --bind 127.0.0.1 --save '' --appendonly no --daemonize yes --pidfile $L/config/redis.pid --dir $L/config --logfile $L/logs/redis.log
echo "LAB READY: $L (fours_customizations $($G -C $L/apps/fours_customizations rev-parse --short HEAD))"
