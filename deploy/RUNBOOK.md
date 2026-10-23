# ps-fours-2 price list split, PHASE 2 (code + data step) - runbook (multax.kit.africa)

Release: `fours_customizations` branch `feat/price-list-split` (commit = the approved one, passed as `FQP_SHA`) on top of
production's `fe153b5` (fqp-1 Quick Price, clean). Kit: `deploy_fqp.sh` + `fqp_check.py` (the fqp-1 blue/green kit with a
new baseline, release ref, file list, smoke and private directories `/root/ps-deploy-multax/`, `/home/frappe/ps-release/`),
then the data step `ps2.py`. Order: PHASE 1 (lead's ps1.py, data only) -> this code release -> `ps2.py apply`.
The kit and ps2.py were rehearsed on the private lab bench (`lab/`, see "Rehearsal").

## What the release changes

Everything is behind `price_lists.split_active()` = the Price List field `custom_company` (Phase 1's Custom Field) exists
and at least one list has a company. Without that, behaviour is fe153b5's. After Phase 1 the split IS active, so the rules
below apply from the moment the code is live.

- NEW `fours_customizations/price_lists.py`, wired in hooks.py:
  - Item Price `has_permission` + `permission_query_conditions`: a user with a Company User Permission that applies to all
    doctypes, Item Price or Price List reads/writes/creates/deletes only rows of ENABLED lists of their companies, and rows
    of lists without a company (Standard Buying, ...) whose item is of their companies' item family ('4S Items' -> 4S;
    'Vox Items' + 'Raw Kitchen Items' -> Vox Lounge / Vox Lounge Nansana; an item of no family -> only users of a company
    outside the families, e.g. Multax). A write/delete checks the stored row and the new values. Administrator, users without
    such a row and Kit POS device sessions keep full access.
  - Price List validate/before_rename: only Administrator or a System Manager without a Company row may rename a list or
    change its title (price_list_name), enabled, selling, buying, currency, price_not_uom_dependent or custom_company; a new selling list needs a
    company; a list stays enabled while an active Kit POS Location or enabled POS Profile uses it.
  - Kit POS Location (active) / POS Profile (enabled): an enabled list of their own company or of no company.
  - Item Price in a company list: allowlist by item family (site_config `fours_price_list_item_guard: 0` turns it off;
    `fours_price_list_families` replaces the family map).
  - Customer / Customer Group / Supplier (/ Supplier Group): a restricted user sets only a blank default list or an
    enabled list of their companies or of no company.
  - Quotation, Sales Order, Delivery Note, Sales Invoice, POS Invoice `before_validate`: a DISABLED selling list becomes
    the company's default list (same currency); line rates are kept (no re-pricing), so a missing price is auto-inserted
    into the enabled list, never into the archive. Same in the form (`public/js/selling_price_list.js`: events
    before_load, company and selling_price_list of a new document).
  - ERPNext's AUTOMATIC price writes (`insert_item_price`: the auto-insert of a missing price while a sales or purchase
    document is priced; `Item.add_price`: a new item's Standard Selling Rate) are wrapped (on import, before_request,
    before_job) and SKIPPED without a message - the document saves, no price is written - when the list is disabled, the
    item is outside a company list's family, or the session user is company-restricted and may not use that row (e.g. a
    Vox Accountant's Purchase Invoice on Standard Buying for a 4S item or an item of no family: before, that failed with
    'Insufficient Permission for Item Price'). The wrap is installed only while ERPNext's functions have the expected
    signatures (`insert_item_price(ctx)`, `Item.add_price(self, price_list=None)`); otherwise ERPNext stays unwrapped and
    one warning per process goes to the `fours_price_lists` log (`verify` checks the wrap: "wrapped: 2/2").
  - Item validate: a NEW item's Item Default rows without a list get the company's default list (Kit POS quick-add and
    ERPNext's Standard Selling Rate then write only the branch's list).
- fours copies of a source document's list (price_adjustment.py, sales_invoice_handler.py DN return, sales_order_handler.py
  DN) use the original only if enabled, else the company default.
- landed_cost_handler: only enabled selling lists of the voucher's company in the company currency; a missing price goes
  to the company default list.
- Quick Price Change (the page's audit log): a company-restricted user (also with Company rows scoped to Item Price /
  Price List) reads only rows of their companies that are not on another company's list.
- Quick Price page API: a company's lists are its own enabled selling lists (default first, no Selling Settings fallback);
  companies and selectable lists follow the desk's Company rule; a company's list is never "shared"; prices of another
  company's item family on lists without a company are hidden; an item outside a list's family is locked with the reason.
- install.after_install (= after_migrate): NEW Custom Field `Company.custom_default_selling_price_list` (Link Price List,
  ignore_user_permissions 1). No patch, no DocPerm / Custom DocPerm change (Vox Accountant stays at 48: the kit's vox-fix).

Note: fours' after_migrate `quick_price.setup.sync_access` re-syncs the Quick Price page roles on every migrate. The kit
puts back the page roles it found before the deploy (`page-roles`), so Phase 1's pause holds until `ps2.py apply`.

## Code release (the fqp-1 kit procedure, new constants)

1. kit-build: `deploy/make_bundle.sh /opt/build/fours-ps-wt <approved sha> <dir>/ps-release.bundle` (prints `FQP_SHA` and
   the sha256). Copy `deploy_fqp.sh`, `fqp_check.py`, `ps2.py` and the bundle to multax `/root/ps-deploy-kit/` (700).
2. `FQP_SHA=<sha> FQP_BUNDLE=/root/ps-deploy-kit/ps-release.bundle ./deploy_fqp.sh precheck` - HEAD must be fe153b5, clean;
   facts gate: the fqp-1 objects exist, the Company field does not yet; Vox Accountant = 48.
3. Window:
   ```
   export FQP_SHA=<sha> FQP_BUNDLE=/root/ps-deploy-kit/ps-release.bundle
   ./deploy_fqp.sh backup
   ./deploy_fqp.sh window        # code, build, migrate (+ vox-fix + page-roles), reload
   ./deploy_fqp.sh verify        # smoke: 28 hook registrations (incl. before_request/before_job and the 5 doctype_js),
                                 # ERPNext wrap 2/2, Company field, per restricted price reader the Item Price list query
                                 # = the Python rule with 0 rows of another company's list / a disabled list, family
                                 # counts of the company lists, consumers, page lists per company; manual list
   ./deploy_fqp.sh postcheck     # other sites unchanged
   ```
   Web probe: Guest `fours_customizations.price_lists.selling_list_replacement` 417 before, 403 after; asset
   `/assets/fours_customizations/js/selling_price_list.js` 404 before, 200 after.

## Data step ps2.py (right after verify)

```
install -d -o frappe -g frappe -m 700 /home/frappe/price-split/kit
install -o frappe -g frappe -m 600 /root/ps-deploy-kit/ps2.py /home/frappe/price-split/kit/ps2.py
run() { su - frappe -c "cd /home/frappe/frappe-bench && PS_MODE=$1 PS_TS=<stamp, e.g. 20261024T0400Z-ps2> bench --site multax.kit.africa console" \
        <<< $'\nexec(open("/home/frappe/price-split/kit/ps2.py").read(), globals())'; }
run plan      # read-only: lists, Company defaults, page roles, references to the archives, gates -> "PLAN: ready for apply"
run apply     # -> "APPLY done"
run verify    # read-only -> "VERIFY: OK"
```
apply: (1) gates - the release is live, Phase 1's three lists exist (company, enabled, selling, UGX), nothing active
(Kit POS Location, POS Profile, Selling Settings, global default, Customer / Customer Group / Supplier / Supplier Group
default, Item Default row; for 'Standard' also a user default) uses Standard Selling (SS), Selling Price Vox Nansana (SPVN)
or the empty 'Standard' list; (2) Company defaults: 4S Industries Limited -> 4S Selling Price, Vox Lounge -> Vox Makindye
Selling Price, Vox Lounge Nansana -> Vox Nansana Selling Price; (3) SS, SPVN and 'Standard' disabled through the release's
guard (`price_lists.validate_price_list`: privileged user, not in use), written with db.set_value so ERPNext's Price List
on_update does not re-stamp `modified` on the archives' Item Prices; SS and 'Standard' keep a blank company, SPVN gets Vox
Lounge Nansana (a site without 'Standard': nothing to do); (4) the page's Custom Role gets Sales Master Manager, 4S
Accountant, Sales User, System Manager, Vox Accountant, Purchase Master Manager; (5) `fours_quick_price:*` redis keys,
ERPNext's `price_list_details` of the archives, the site cache. verify also checks the three archives (disabled, company).
Every changed value is saved first in `/home/frappe/price-split/<stamp>/ps2-before.json` (+ `.sha256`, 600); log
`ps2-log.txt`. Re-runs skip what is done; a value that is neither the before-image nor the target stops (drift).
Prints names of lists/companies/roles, counts and 8-hex user hashes only.

### After ps2 (browser)
1. A Vox Accountant (Makindye): Item Price list = Makindye list + Standard Buying rows of bar items; no 4S item, no
   Nansana row, no Standard Selling row (disabled archive).
2. A 4S Accountant: only 4S-list rows and 4S items on Standard Buying.
3. Quick Price page: each company shows its own list only; save one price (owner's choice).
4. Amend one pre-split non-POS invoice: the list switches to the company's list, rates stay; change qty: no
   'Price List Standard Selling is disabled' error.
5. Desk tabs open since before the window: hard reload (stale `selling_price_list` default; the form script switches new
   documents anyway).

## Rollback

- Data step: `run rollback` (same stamp) - page roles, SS/SPVN/'Standard' enabled + company, Company defaults back from
  the before-image (reverse order), caches. Safe after a partial apply; a value changed since apply stops for a decision.
  plan and rollback do not import the release's code, so they also run on fe153b5.
- Code: `FQP_SHA=<sha> ./deploy_fqp.sh rollback` - FIRST a read-only gate (`fqp_check.py ps2-state`): it STOPS, before
  anything changes, while ps2 is applied (a Company default list set, or SS / SPVN disabled) - fe153b5 has no replace
  hooks, so amendments and mapped documents of pre-split documents would fail on the disabled archive.
  `FQP_ACK_KEEP_PS2_DATA=1` overrides (lead decision only; then `run rollback` on fe153b5 and
  `fqp_check.py rollback-cleanup --site multax.kit.africa --apply`). Then: previous tree back, migrate multax, vox-fix,
  page roles back to the pre-deploy ones (the old page code must not run against Phase 1 data: it stays paused),
  `rollback-cleanup` removes the Company field only while every company value is blank, build, reload (probe 417).
  Phase 1's data stays.
- Order for a full undo: `ps2.py rollback` -> `deploy_fqp.sh rollback` -> Phase 1's own rollback (lead's ps1.py).

## Residuals (owner decisions; not in this release)

- Not a hard confidentiality boundary: ERPNext whitelisted readers (get_item_details, apply_price_list, POS get_items),
  script reports ('Item Prices', 'Item Price Stock', tally 'Item Prices Report') and dashboard counts read prices without
  this rule; Kit POS devices running as Administrator and staff with all locations see every location's catalogue.
- Stock Settings (`update_existing_price_list_rate`, `auto_insert_price_list_rate_if_missing`) stay writable by Vox
  Accountant (lead: no guard needed).
- Other ENABLED lists without a company (Standard Buying, Standard Buying Dollar, Standard Selling Dollar, CONGO BIG
  CUSTMERS) stay shared: a restricted user writes there only items of their companies' families, and only an
  administrator may change such a list's settings or title. A POS Profile / Kit POS Location may still use one.
- Users without an applicable Company row stay unrestricted (Administrator, 2279435b, e79777e7, c2bf681c unless Phase 1
  gives it a row).
- The family allowlist REFUSES, with a message naming the item, its group and the list, every DIRECT price write of an
  item outside a company list's family: desk / import / API, the Quick Price page, Kit POS quick-add of an item in another
  family. AUTOMATIC writes are SKIPPED WITHOUT NOTICE (the document saves, no price is written): ERPNext's auto-insert on
  a sale or purchase, a new item's Standard Selling Rate whose list (Item Default or Selling Settings) is of another
  family or disabled, a restricted writer's auto-insert of a row they may not use, and the landed cost handler's missing
  price of another family. So an item can stay unpriced in a till list: price it on the Quick Price page.
- 4S EFRIS (/opt/build/4s-efris-setup lib4s.php) accepts only 'Standard Selling' prices: update before its go-live.

## Rehearsal (lab only)

`lab/` in `kasc-bom-lab` (private bench `/home/frappe/fqp-bench`, never the shared lab bench): `rehearse.sh <kit> reset`
(fqpm.lab recloned from labvox, migrated at fe153b5, production Item Price rules + lab users, Vox owner removals, Phase 1
simulated by `ps1_standin.py`), `forward` (kit precheck..postcheck, then ps2 plan/apply/verify), `rollback` (ps2
rollback, kit rollback, rollback-cleanup), `reforward`, `wrongorder` (ps2 applied -> the kit's rollback must stop before
the swap; with FQP_ACK_KEEP_PS2_DATA=1 it goes back, then ps2 rollback on fe153b5 and rollback-cleanup recover). Tests: `push-tree.sh`, then `bench --site fqpm.lab run-tests
--module fours_customizations.tests.test_price_lists` (and `test_quick_price_update`); mutants: `mutate_ps.py`.
