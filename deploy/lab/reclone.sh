#!/bin/bash
# fqp-1 LAB ONLY (root inside kasc-bom-lab): drop one private-bench lab site and clone it again from its shared-bench source.
# usage: reclone.sh fqpm.lab|fqpd.lab|fqpdd.lab
set -euo pipefail
L=/home/frappe/fqp-bench S=/home/frappe/frappe-bench
case $1 in fqpm.lab) SRC=labvox.kit.africa M=0 ;; fqpd.lab) SRC=labdemo.kit.africa M=0 ;; fqpdd.lab) SRC=labddamu.kit.africa M=1 ;; *) echo "unknown $1" >&2; exit 2 ;; esac
export MYSQL_PWD=$(cat /root/kbom-private/db-root)
c=$L/sites/$1/site_config.json
if [ -f "$c" ]; then
  DB=$(python3 -c "import json;print(json.load(open('$c'))['db_name'])")
  case "$DB" in _fqp[0-9a-f]*) mariadb -uroot -e "DROP DATABASE IF EXISTS \`$DB\`; DROP USER IF EXISTS '$DB'@'localhost', '$DB'@'%';" ;; *) echo "unexpected db $DB" >&2; exit 2 ;; esac
  find $L/sites/$1 -mindepth 1 -delete; rmdir $L/sites/$1
fi
DB=_fqp$(openssl rand -hex 6); PW=$(openssl rand -hex 16)
SDB=$(python3 -c "import json;print(json.load(open('$S/sites/$SRC/site_config.json'))['db_name'])")
mariadb -uroot -e "CREATE DATABASE \`$DB\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; CREATE USER '$DB'@'localhost' IDENTIFIED BY '$PW'; CREATE USER '$DB'@'%' IDENTIFIED BY '$PW'; GRANT ALL PRIVILEGES ON \`$DB\`.* TO '$DB'@'localhost'; GRANT ALL PRIVILEGES ON \`$DB\`.* TO '$DB'@'%';"
mariadb-dump -uroot --single-transaction --routines --triggers "$SDB" | mariadb -uroot "$DB"
mkdir -p $L/sites/$1/{private/backups,private/files,public/files,logs,locks}
python3 - "$S/sites/$SRC/site_config.json" "$c" "$DB" "$PW" "$M" <<'PY'
import json, sys
src = json.load(open(sys.argv[1]))
c = {k: v for k, v in src.items() if not k.startswith("saleslive")}
c.update(db_name=sys.argv[3], db_user=sys.argv[3], db_password=sys.argv[4], pause_scheduler=1, mute_emails=1, developer_mode=0)
if sys.argv[5] == "1":
	c["maintenance_mode"] = 1
json.dump(c, open(sys.argv[2], "w"), indent=1)
PY
chown -R frappe:frappe $L/sites/$1; chmod 600 $c
echo "recloned $SRC -> $1"
