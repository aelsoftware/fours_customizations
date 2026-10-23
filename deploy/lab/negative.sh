#!/bin/bash
# fqp-1 / ps-fours-2 LAB ONLY: the kit's gates must stop (exit 2) on a bad state. Run after 'rehearse.sh <kit> reset' (baseline code).
# usage: negative.sh <kit dir> <lab Vox count>
set -uo pipefail
K=$1 N=$2
L=/home/frappe/fqp-bench
export PATH=/root/fqp/bin:$PATH
. "$K/release.env"; export FQP_SHA FQP_BUNDLE
DEP="$K/deploy_fqp.lab.sh"
fails=0
expect_stop() {  # expect_stop <label> <pattern> <cmd...>
  local label=$1 pat=$2; shift 2
  local out rc
  out=$("$@" 2>&1); rc=$?
  if [ $rc -eq 2 ] && grep -q -- "$pat" <<< "$out"; then echo "STOPPED as expected: $label ($(grep -m1 -o -- "$pat.\{0,60\}" <<< "$out"))"
  else echo "NOT STOPPED: $label (rc $rc)"; fails=$((fails + 1)); fi
}
asf() { su - frappe -c "cd $L && $*"; }

# 1. production code changed after the baseline (a local edit)
asf "echo '# local edit' >> apps/fours_customizations/README.md"
expect_stop "local change in apps/fours_customizations" "local changes" env FQP_NEW_TS=1 "$DEP" precheck
asf "git -c safe.directory='*' -C apps/fours_customizations checkout -q -- README.md"

# 2. the Vox Accountant baseline is not the expected count
bash "$K/lab/mklab.sh" "$K" $((N + 1)) > /dev/null
expect_stop "Vox Accountant rows differ from the expected count" "Vox Accountant Custom DocPerm rows are" env FQP_NEW_TS=1 "$DEP" precheck
bash "$K/lab/mklab.sh" "$K" "$N" > /dev/null

# 3. a release commit that is not the one in the bundle
expect_stop "FQP_SHA not in the bundle" "release commit is not available" env FQP_NEW_TS=1 FQP_SHA=0123456789abcdef0123456789abcdef01234567 "$DEP" precheck

# 4. a baseline Vox Accountant row missing after migrate is reported, never invented
TS=$(asf "cat /home/frappe/ps-release-lab/DEPLOY_TS")
F=/home/frappe/ps-release-lab/$TS/facts-neg.json
asf "cd sites && ../env/bin/python /home/frappe/ps-release-lab/kit/fqp_check.py facts --site fqpm.lab --out $F" > /dev/null
asf "python3 -c \"import json; d=json.load(open('$F')); d['vox_accountant_rows']['fakefqp01'] = dict(parent='Item', role='Vox Accountant', permlevel=0, if_owner=0, read=1); json.dump(d, open('$F', 'w'))\""
expect_stop "a baseline row missing after migrate" "baseline Vox Accountant rows are missing" asf "cd sites && ../env/bin/python /home/frappe/ps-release-lab/kit/fqp_check.py vox-fix --site fqpm.lab --baseline $F --apply"
asf "rm -f $F"

# 5. ps-fours-2: the release's Company field already on the site = not the pre-release baseline
asf "bench --site fqpm.lab execute frappe.custom.doctype.custom_field.custom_field.create_custom_field --kwargs \"{'doctype': 'Company', 'df': {'fieldname': 'custom_default_selling_price_list', 'fieldtype': 'Link', 'options': 'Price List', 'label': 'Default Selling Price List'}}\"" > /dev/null
expect_stop "the Company field exists before the release" "already exists" env FQP_NEW_TS=1 "$DEP" precheck
asf "bench --site fqpm.lab execute frappe.delete_doc --kwargs \"{'doctype': 'Custom Field', 'name': 'Company-custom_default_selling_price_list', 'force': 1}\"" > /dev/null

# 6. after all of that, a clean precheck passes (nothing was left behind)
out=$(env FQP_NEW_TS=1 "$DEP" precheck 2>&1); rc=$?
[ $rc -eq 0 ] && echo "clean precheck passes" || { echo "clean precheck FAILED (rc $rc)"; echo "$out" | tail -5; fails=$((fails + 1)); }
echo "negative probes: $fails unexpected"
exit $fails
