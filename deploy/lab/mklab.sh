#!/bin/bash
# fqp-1 / ps-fours-2 LAB ONLY: make deploy_fqp.lab.sh from deploy_fqp.sh for the private lab bench. The ONLY differences (printed
#   as a diff): BENCH /home/frappe/frappe-bench -> /home/frappe/fqp-bench; private dirs ps-release -> ps-release-lab,
#   /root/ps-deploy-multax -> /root/ps-deploy-lab; site names multax.kit.africa -> fqpm.lab, demo.kit.africa -> fqpd.lab, ddamu.kit.africa -> fqpdd.lab;
#   EXPECT_SITES -> the lab's three sites; EXPECT_VOX 48 -> the lab baseline (argument 2: the stand-in has fewer Vox rows);
#   asf() puts the lab's node (/opt/node/bin) on PATH for bench build (production's bench user has node on its PATH)
# usage: mklab.sh <kit dir> <lab Vox Accountant row count>
set -euo pipefail
K=$1 N=$2
sed -e 's|^BENCH=/home/frappe/frappe-bench$|BENCH=/home/frappe/fqp-bench|' \
    -e 's|^RUN=/home/frappe/ps-release$|RUN=/home/frappe/ps-release-lab|' -e 's|^LOGDIR=/root/ps-deploy-multax$|LOGDIR=/root/ps-deploy-lab|' \
    -e 's|^EXPECT_SITES=.*|EXPECT_SITES="fqpd.lab fqpdd.lab fqpm.lab"|' -e "s|^EXPECT_VOX=48 |EXPECT_VOX=$N |" \
    -e 's|multax\.kit\.africa|fqpm.lab|g' -e 's|demo\.kit\.africa|fqpd.lab|g' -e 's|ddamu\.kit\.africa|fqpdd.lab|g' \
    -e 's|^asf() { su - frappe -c "set -o pipefail; |asf() { su - frappe -c "export PATH=/opt/node/bin:\\$PATH; set -o pipefail; |' \
    "$K/deploy_fqp.sh" > "$K/deploy_fqp.lab.sh"
chmod +x "$K/deploy_fqp.lab.sh"
diff "$K/deploy_fqp.sh" "$K/deploy_fqp.lab.sh" | grep '^[<>]' | cut -c1-160 || true
