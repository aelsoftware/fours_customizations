#!/bin/bash
# fqp-1 LAB ONLY (root inside kasc-bom-lab): serve the multax stand-in of the private bench for the browser check:
# frappe's werkzeug server (static assets included; debugger off with CI=1) on 0.0.0.0:18223, only reachable from the lab
# bridge (kasc-bom-tills). usage: serve.sh start|stop
L=/home/frappe/fqp-bench
case ${1:-} in
  start) runuser -u frappe -- bash -c "cd $L/sites && CI=1 exec setsid ../env/bin/python -c \"import frappe.app; frappe.app.serve(port=18223, site='fqpm.lab', sites_path='.', no_reload=True)\" >> $L/logs/serve.log 2>&1 & echo \$! > $L/config/serve.pid"
         sleep 4; curl -s -o /dev/null -w 'ping %{http_code}\n' http://127.0.0.1:18223/api/method/ping ;;
  stop) [ -f $L/config/serve.pid ] && kill "$(cat $L/config/serve.pid)" 2>/dev/null; rm -f $L/config/serve.pid; echo stopped ;;
  *) echo "usage: serve.sh start|stop" >&2; exit 2 ;;
esac
