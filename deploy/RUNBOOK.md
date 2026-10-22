# fqp-1 Quick Price Update - deploy runbook (multax.kit.africa)

Release: `fours_customizations` branch `feat/quick-price-update` (commit = the approved one, passed as `FQP_SHA`) on top of
production's `7da0d29` (clean). Kit: `deploy_fqp.sh` + `fqp_check.py` (this directory). The lead deploys; the kit was
rehearsed end to end on a private lab bench (`lab/`, see "Rehearsal").

## What the release changes

- New: page `quick-price-update` (Quick Price Update), DocType `Quick Price Change` (audit), Workspace `Pricing` + its
  Workspace Sidebar and Desktop Icon, Item Price list button (`public/js/quick_price_item_price_list.js`), API module
  `fours_customizations.quick_price`.
- hooks.py: `doctype_list_js` (Item Price), `has_permission` / `permission_query_conditions` for Quick Price Change,
  `ignore_links_on_delete = ["Quick Price Change"]`; `install.after_install` (= after_migrate) calls
  `quick_price.setup.sync_access()`, which writes the page's **Custom Role** and the roles of the Pricing workspace and
  icon = every role with full Item Price read except Kit TV Viewer, plus System Manager. Read from the site, never written:
  no DocPerm / Custom DocPerm is created or changed by the release.
- No patch, no new dependency, no Custom Field, no change to an existing DocType. No site setting is changed.

## Rules the kit enforces

- The bench is SHARED (ddamu, demo, erp.kit.africa, jamissang.efris.kit.africa, jamissang.kit.africa, multax.kit.africa,
  pma-validation.local, tobi). Only `bench --site multax.kit.africa migrate`; never `bench migrate` without `--site`,
  never `--site all`, never ddamu (it stays in maintenance), never `bench restart` / `bench update`.
- precheck reads every site's installed apps: if a site other than multax has `fours_customizations`, the kit stops
  (that site would get the code without a migrate); `FQP_ACK_OTHER_FOURS=<sites>` is the lead's explicit decision.
- precheck: `apps/fours_customizations` HEAD = `7da0d29`, `git status` clean; the release descends from it and changes no
  patches/dependencies; nothing else pending for any app of multax (patches or files migrate would import), else stop
  (`FQP_ACK_SYNC=<apps>` after reading the list).
- Backup of multax (`--with-files`) and of the app tree (tar + sha256) before the swap.
- Code swap = one `mv --exchange -T` (two renames as fallback); previous tree kept as `apps/.fours_customizations-prev-<TS>`.
- `bench build --app fours_customizations` only; the other apps' asset manifest/links are compared before/after.
- After migrate: **Vox Accountant Custom DocPerm rows must still be 48**. The baseline rows (saved in `backup` as
  `facts-before.json`) are the legitimate ones; rows the migrate re-adds (fours' and kit_pos' own after_migrate re-add
  Vox Accountant grants, e.g. Sales Invoice read and Voided Bill) are removed, changed rows restored, and the count must
  be 48 again (exit 2 otherwise; a missing baseline row is reported, not invented). Every other role's Custom DocPerm rows,
  `site_config.json` (except the kit's own `pause_scheduler`) and the kit_pos switches (site_config `kit_pos*`/`vox_*`
  keys, `Kit POS*` settings values) must be unchanged.
- Reload = gunicorn HUP (graceful, wait for old workers), workers group restart, `clear-cache` + `scheduler resume` of
  multax only.
- postcheck: every other site (jamissang.kit.africa included) fingerprinted before and after (patch log, DocTypes, Custom
  DocPerm rows + hash, schema size, apps, scheduler, maintenance, site_config hash): must be identical; ddamu still in
  maintenance.

## Before the window

1. On kit-build: `deploy/make_bundle.sh /opt/build/fours-qp-wt <approved sha> <dir>/fqp-release.bundle` (prints
   `FQP_SHA` and the bundle's sha256). Copy `deploy_fqp.sh`, `fqp_check.py` and the bundle to a root-only directory on
   multax (e.g. `/root/fqp-deploy-kit/`, 700). Alternatively the lead pushes the branch and sets `FQP_REF`.
2. `FQP_SHA=<sha> FQP_BUNDLE=/root/fqp-deploy-kit/fqp-release.bundle ./deploy_fqp.sh precheck` - read every line;
   it prints the sites with fours_customizations, the pending list, the Vox Accountant count (must be 48), the Item Price
   read / write roles, the other-site fingerprints. It also previews what the release migrate imports
   (5 new records: DocType Quick Price Change, Page quick-price-update, Workspace / Workspace Sidebar / Desktop Icon Pricing).

## Window (about 2 minutes of work, scheduler of multax paused for about 1 minute)

```
export FQP_SHA=<sha> FQP_BUNDLE=/root/fqp-deploy-kit/fqp-release.bundle
./deploy_fqp.sh backup
./deploy_fqp.sh window        # = code, build, migrate, reload (stops at the first failed gate)
./deploy_fqp.sh verify        # read-only smoke + manual browser list
./deploy_fqp.sh postcheck     # other sites unchanged
```

Phases can also be run one by one (`code`, `build`, `migrate`, `reload`). Each phase is re-runnable after a fix;
`status` summarises. Logs: `/root/fqp-deploy-multax/`; private facts: `/home/frappe/fqp-release/<TS>/`.

Window between `code` and `migrate`: the new files are on disk while the schema is the old one. Nothing breaks:
the Item Price list button appears only for users who already have the page (none until the migrate creates it), and
the API refuses everyone (no page record). Keep the gap short anyway.

The web check (Guest calls to `fours_customizations.quick_price.api.get_context`) answers 417 before the swap and 403
after it. It is not proof that every worker was reloaded (an old worker imports the new module from disk on demand); the
reload phase proves that by waiting until every old gunicorn worker pid has exited. With `--preload` the new workers are
forked from the gunicorn master, which has imported frappe only (Frappe imports an app's modules per request, per site),
so they load fours_customizations from the new tree - the same HUP procedure as the 2026-10-21 Jamissang deploy on this
bench, and what the lab rehearsal (gunicorn --preload) shows.

## Manual checks after `verify` (browser)

1. A Vox Accountant: the Item Price list shows "Quick Price Update"; the page lists Vox Lounge / Vox Lounge Nansana
   (their Company User Permissions); the header says "Standard Selling is used by 4S Industries Limited, Vox Lounge and Vox
   Lounge Nansana: a change applies to all of them".
2. A 4S Accountant: the page opens for 4S Industries Limited; every price cell is locked (no Item Price write today).
3. A Kit TV Viewer: no button; the page says "Not permitted".
4. Desk search "Quick Price" offers the page to users who have it; the Pricing workspace (desk icon "Pricing") shows the
   shortcut.
5. One real change by an authorised user, on an item the owner chooses: the Item Price changes, a Quick Price Change
   row appears, and the Item Price has a Version (track_changes is on for Item Price).

## Rollback

`FQP_SHA=<sha> ./deploy_fqp.sh rollback`: previous tree back (from `apps/.fours_customizations-prev-<TS>`, or rebuilt
from the tree tarball, sha256 checked), `bench --site multax.kit.africa migrate` (Frappe removes the page, the DocType
record, the workspace/sidebar/icon whose files left), the Vox Accountant rows back to the baseline again, the release's
Custom Role / Pricing records removed if still there, `bench build --app fours_customizations`, reload (web check: 417).
What stays: the `tabQuick Price Change` table and its rows (audit); prices changed through the page keep their new rates
(their Versions show the old ones). Restoring the backup is a platform-owner decision (it loses every transaction since).

## Who can update prices (unchanged by this release - the owner decides)

The page shows every cell the user may change according to Item Price permissions and their User Permissions; it never
grants anything. On multax today (2026-10-22 facts): Item Price write/create = Sales Master Manager, Purchase Master
Manager, Vox Accountant; 4S Accountant and Sales User read only. So **no 4S user can update prices** today: they see the
page with every cell locked. To change that, the owner grants Item Price write (and create, for prices that do not exist
yet) to the chosen role in Role Permissions Manager; after a change of who may READ Item Prices, run
`bench --site multax.kit.africa execute fours_customizations.quick_price.setup.sync_access` so the page/workspace roles
follow (it also runs on every migrate). Note: Item Price has one list for many companies: a writer of 'Standard Selling'
changes the price for Vox Lounge, 4S and Vox Lounge Nansana at once (the page says so before saving).

## Rehearsal (lab only)

`lab/` holds the private-bench rehearsal in `kasc-bom-lab` (never the shared lab bench): `setup.sh` (private bench with
clones of labvox/labdemo/labddamu as multax/demo/ddamu stand-ins), `mklab.sh` (lab copy of the kit: bench path, site
names, lab Vox count; printed diff), `supervisorctl` (stand-in: gunicorn --preload + one worker), `rehearse.sh reset |
forward | rollback | reforward`, `reclone.sh`, `teardown.sh`. Tests, browser check and mutants: `push-tree.sh`,
`serve.sh`, `seed_ui.py`, `ui_check.mjs` (headless Chromium in kasc-bom-tills), `perf.py`, `mutate.py`, `race_probe.py`
(two real connections: a price committed by another user after this request's snapshot is refused, not duplicated),
`negative.sh` (the kit's gates stop on a local code change, a Vox count other than the baseline, a wrong FQP_SHA and a
baseline Vox row missing after migrate).
