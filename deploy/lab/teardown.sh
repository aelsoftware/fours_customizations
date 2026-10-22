#!/bin/bash
# fqp-1 LAB ONLY: remove everything deploy/lab/setup.sh created in kasc-bom-lab (root inside the container): processes, the
# _fqp* databases + users, the private bench, the lab upstream, /root/fqp, the rehearsal's private dirs. The shared bench is
# not touched.
set -uo pipefail
L=/home/frappe/fqp-bench
[ -x /root/fqp/bin/supervisorctl ] && /root/fqp/bin/supervisorctl stop all >/dev/null 2>&1
for p in serve; do [ -f $L/config/$p.pid ] && kill "$(cat $L/config/$p.pid)" 2>/dev/null; done
[ -f $L/config/redis.pid ] && kill "$(cat $L/config/redis.pid)" 2>/dev/null
export MYSQL_PWD=$(cat /root/kbom-private/db-root)
for c in $L/sites/*/site_config.json; do
  [ -f "$c" ] || continue
  DB=$(python3 -c "import json;print(json.load(open('$c'))['db_name'])")
  case "$DB" in _fqp[0-9a-f]*) mariadb -uroot -e "DROP DATABASE IF EXISTS \`$DB\`; DROP USER IF EXISTS '$DB'@'localhost', '$DB'@'%';" && echo "dropped $(dirname "$c" | xargs basename)";; *) echo "skip unexpected db in $c";; esac
done
[ -d $L ] && find $L -mindepth 1 -delete && rmdir $L
[ -d /home/frappe/fqp-upstream.git ] && find /home/frappe/fqp-upstream.git -mindepth 1 -delete && rmdir /home/frappe/fqp-upstream.git
[ -d /root/fqp ] && find /root/fqp -mindepth 1 -delete && rmdir /root/fqp
for d in /home/frappe/fqp-release-lab /root/fqp-deploy-lab; do [ -d $d ] && find $d -mindepth 1 -delete && rmdir $d; done   # rehearsal
ls -d /home/frappe/fqp* /root/fqp* /tmp/fqp* 2>/dev/null | sed 's/^/LEFTOVER path: /'
mariadb -uroot -N -e "SHOW DATABASES LIKE '\\_fqp%'; SELECT user FROM mysql.user WHERE user LIKE '\\_fqp%'" | sed 's/^/LEFTOVER: /'
pgrep -u frappe -f fqp-bench | sed 's/^/LEFTOVER pid: /'
echo "teardown done"
