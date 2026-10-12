"""Offline tests for the explicit, no-commit Vox menu metadata migration."""

from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe


SPEC = spec_from_file_location("vox_goat_menu_activation_test_target",
    "/home/frappe/frappe-bench/sites/multax.kit.africa/private/files/vox_goat_menu_activate_20260928.py")
module = module_from_spec(SPEC)
SPEC.loader.exec_module(module)
D = frappe._dict


def fixture():
    items, boms, prices = {}, {}, {}
    for code, (name, rate, bom_name) in module.MENU.items():
        items[code] = D(name=code, item_code=code, item_name=name, disabled=1, is_stock_item=0,
            is_sales_item=1, is_purchase_item=0, stock_uom="Pc(s)", sales_uom="Pc(s)",
            allow_negative_stock=0, opening_stock=0, has_variants=0, has_batch_no=0, has_serial_no=0,
            is_fixed_asset=0, default_bom=bom_name, item_defaults=[D(company=module.COMPANY)],
            uoms=[D(uom="Pc(s)", conversion_factor=1)])
        boms[bom_name] = D(name=bom_name, item=code, company=module.COMPANY, docstatus=1, is_active=1,
            is_default=1, quantity=25, uom="Pc(s)", currency="UGX", transfer_material_against="Work Order",
            items=[D(item_code=module.RAW, uom="Kg", stock_uom="Kg", qty=10, stock_qty=10,
                     conversion_factor=1, source_warehouse=module.WAREHOUSE, include_item_in_manufacturing=1)])
        for index, price_list in enumerate(("Standard Selling", "Selling Price Vox Nansana")):
            price_name = f"{code}-PRICE-{index}"
            prices[price_name] = D(name=price_name, item_code=code, price_list=price_list,
                currency="UGX", uom="Pc(s)", selling=1, buying=0, price_list_rate=rate)
    items[module.RAW] = D(name=module.RAW, is_stock_item=1, stock_uom="Kg", disabled=0, is_sales_item=0,
        is_purchase_item=1, item_group="VXN Kitchen Ingredients", has_variants=0, has_batch_no=0, has_serial_no=0)
    items[module.LEGACY] = D(name=module.LEGACY, is_stock_item=0)
    warehouses = {module.WAREHOUSE: D(name=module.WAREHOUSE, company=module.COMPANY, disabled=0, is_group=0)}
    locations, profiles = {}, {}
    for name in ("Main Bar", "Mini Bar"):
        warehouse = name + " - VXN"
        locations[name] = D(name=name, company=module.COMPANY, warehouse=warehouse,
                            selling_price_list="Standard Selling", pos_profile=name)
        profiles[name] = D(name=name, company=module.COMPANY, warehouse=warehouse, disabled=0,
                           update_stock=1, selling_price_list="Standard Selling")
        warehouses[warehouse] = D(name=warehouse, company=module.COMPANY, disabled=0, is_group=0)
    return {
        "items": items, "boms": boms, "prices": prices,
        "bom_outputs": [D(name=name, item=bom.item, docstatus=1) for name, bom in boms.items()],
        "history": {f"{doctype}.{field}": {code: 0 for code in module.MENU}
                    for doctype, fields in module.HISTORY_FIELDS.items() for field in fields},
        "pos_history": 0, "bins": [], "ids": [], "raw_ledger": [],
        "pilot_sale": {"name": "4929", "is_suspend": 1}, "pilot_invoice": {"name": "SI-2640-198", "docstatus": 0},
        "negative_stock": 1, "settings": {"auto_reserve_stock": 0, "valuation_method": "LIFO"},
        "company_automations": 0, "warehouses": warehouses, "locations": locations, "profiles": profiles,
        "cost_center": D(name=module.COST_CENTER, company=module.COMPANY, disabled=0, is_group=0),
        "configuration": {
            "vox_kitchen_modifiers": {module.COMPANY: True}, "vox_goat_work_orders": {module.COMPANY: True},
            "kit_pos_retired_menu_items": {module.COMPANY: [module.LEGACY]},
            "kit_pos_ingredient_item_groups": {module.COMPANY: ["VXN Kitchen Ingredients"]},
        },
    }


def published():
    return {code: [
        {"name": "Sides", "modifiers": [{"name": label, "price": 10000} for label in sorted(module.SIDES)]},
        {"name": "Extra Toppings", "modifiers": [{"name": "Extra " + label, "price": 5000} for label in sorted(module.SIDES)]},
    ] for code in module.MENU}


class Item:
    def __init__(self, values, store, saved):
        self.__dict__.update(deepcopy(values))
        self._store = store
        self._saved = saved

    def as_dict(self):
        return D({key: deepcopy(value) for key, value in self.__dict__.items() if not key.startswith("_")})

    def save(self, *args, **kwargs):
        # Native validation sees the approved, already-migrated stock flag.
        assert self._store[self.name].is_stock_item == self.is_stock_item == 1
        assert not args and not kwargs and "flags" not in self.__dict__
        self._store[self.name] = self.as_dict()
        self._saved.append(self.name)


class TestVoxGoatMenuActivation(TestCase):
    def setUp(self):
        from fours_customizations import vox_modifiers, vox_work_orders

        self.before = fixture()
        self.db = SimpleNamespace(transaction_writes=0, auto_commit_on_many_writes=False,
            _disable_transaction_control=False, commit=Mock(), rollback=Mock(), sql=Mock(return_value=[]),
            set_value=Mock(), has_column=Mock(return_value=True))
        self.frappe = SimpleNamespace(db=self.db, local=SimpleNamespace(site=module.SITE),
            session=SimpleNamespace(user="Administrator"), clear_document_cache=Mock(), get_doc=Mock())
        self.enterContext(patch.object(module, "frappe", self.frappe))
        self.schema = self.enterContext(patch.object(module, "_schema_and_hooks_ready"))
        self.enterContext(patch.object(vox_modifiers, "preview_publications", return_value=published()))
        self.recipe = self.enterContext(patch.object(vox_work_orders, "_recipe"))

    def test_valid_exact_state_is_ready_without_writing(self):
        self.assertEqual(module._preflight(self.before), "ready")
        self.db.set_value.assert_not_called()
        self.db.commit.assert_not_called()

    def test_every_native_history_dimension_blocks_metadata_change(self):
        for key in self.before["history"]:
            altered = deepcopy(self.before)
            altered["history"][key]["VXN-DRY-GOAT"] = 1
            with self.subTest(history=key), self.assertRaisesRegex(RuntimeError, "transaction history"):
                module._preflight(altered)

    def test_pos_history_reservations_and_partial_activation_are_rejected(self):
        cases = []
        value = deepcopy(self.before)
        value["pos_history"] = 1
        cases.append(value)
        value = deepcopy(self.before)
        value["bins"] = [D(item_code="VXN-DRY-GOAT", reserved_qty_for_production=1)]
        cases.append(value)
        value = deepcopy(self.before)
        value["items"]["VXN-DRY-GOAT"].disabled = 0
        cases.append(value)
        for value in cases:
            with self.assertRaises(RuntimeError):
                module._preflight(value)

    def test_outward_native_alias_and_extra_bom_are_rejected(self):
        self.before["ids"] = [D(name="1000", ref_doctype="Item", ref_name="VXN-DRY-GOAT",
            native_doctype="Item", native_name="VX-0128")]
        with self.assertRaisesRegex(RuntimeError, "alias or redirect"):
            module._preflight(self.before)
        self.before["ids"] = []
        self.before["bom_outputs"].append(D(name="OTHER-BOM", item="VXN-DRY-GOAT", docstatus=0))
        with self.assertRaisesRegex(RuntimeError, "output BOM"):
            module._preflight(self.before)

    def test_bom_stock_units_sources_operations_and_loss_are_exact(self):
        code = "VXN-DRY-GOAT"
        bom_name = module.MENU[code][2]
        for fieldname, value in (("source_warehouse", "Other - VXN"), ("conversion_factor", 10),
                                 ("stock_qty", 100), ("stock_uom", "Nos"), ("include_item_in_manufacturing", 0)):
            altered = deepcopy(self.before)
            altered["boms"][bom_name]["items"][0][fieldname] = value
            with self.subTest(field=fieldname), self.assertRaises(RuntimeError):
                module._preflight(altered)
        for fieldname, value in (("operations", ["cook"]), ("process_loss_percentage", 1),
                                 ("scrap_items", ["scrap"]), ("transfer_material_against", "Job Card")):
            altered = deepcopy(self.before)
            altered["boms"][bom_name][fieldname] = value
            with self.subTest(field=fieldname), self.assertRaises(RuntimeError):
                module._preflight(altered)

    def test_wrong_warehouse_company_and_automatic_stock_modes_are_rejected(self):
        altered = deepcopy(self.before)
        altered["warehouses"][module.WAREHOUSE].company = "Vox Lounge"
        with self.assertRaises(RuntimeError):
            module._preflight(altered)
        altered = deepcopy(self.before)
        altered["settings"]["auto_reserve_stock"] = 1
        with self.assertRaises(RuntimeError):
            module._preflight(altered)
        altered = deepcopy(self.before)
        altered["company_automations"] = 1
        with self.assertRaises(RuntimeError):
            module._preflight(altered)

    def _migration(self):
        self.store = deepcopy(self.before["items"])
        self.saved = []
        self.frappe.get_doc.side_effect = lambda doctype, name, **kwargs: Item(self.store[name], self.store, self.saved)

        def set_value(doctype, name, field, value, **kwargs):
            self.assertEqual((doctype, field, value, kwargs), ("Item", "is_stock_item", 1, {"update_modified": True}))
            self.assertIn(name, module.MENU)
            self.store[name][field] = value

        self.db.set_value.side_effect = set_value

        def snapshot(**kwargs):
            self.assertEqual(kwargs, {"for_update": True})
            return {**deepcopy(self.before), "items": deepcopy(self.store)}

        self.enterContext(patch.object(module, "_lock_state"))
        self.enterContext(patch.object(module, "_snapshot", side_effect=snapshot))

    def test_guarded_metadata_change_is_exact_and_native_saves_do_not_bypass_validation(self):
        self._migration()
        preview = module.preview_locked()
        self.assertEqual(preview["state"], "ready")
        self.db.set_value.assert_not_called()
        result = module.prepare_unused_menu_items(expected_before_digest=preview["before_digest"])
        self.assertEqual(result["state"], "prepared")
        self.assertFalse(result["committed"])
        self.assertEqual(self.saved, list(module.MENU))
        self.assertEqual(self.db.set_value.call_count, 2)
        self.assertEqual(self.recipe.call_count, 2)
        for code in (module.RAW, module.LEGACY):
            self.assertEqual(self.store[code], self.before["items"][code])
        self.db.commit.assert_not_called()
        self.db.rollback.assert_not_called()
        self.assertEqual(self.frappe.session.user, "Administrator")

    def test_stale_digest_wrong_actor_dirty_transaction_cannot_migrate(self):
        self._migration()
        with self.assertRaisesRegex(RuntimeError, "State changed"):
            module.prepare_unused_menu_items(expected_before_digest="0" * 64)
        self.frappe.session.user = "cashier@example.com"
        with self.assertRaises(RuntimeError):
            module.prepare_unused_menu_items(expected_before_digest=module._digest(self.before))
        self.frappe.session.user = "Administrator"
        self.db.transaction_writes = 1
        with self.assertRaises(RuntimeError):
            module.prepare_unused_menu_items(expected_before_digest=module._digest(self.before))
        self.db.set_value.assert_not_called()

    def test_approved_bom_is_preserved_and_other_menu_fields_cannot_change(self):
        after = deepcopy(self.before)
        for code in module.MENU:
            after["items"][code].disabled = 0
            after["items"][code].is_stock_item = 1
        module._assert_preserved(self.before, after)
        after["items"]["VXN-DRY-GOAT"].item_name = "Different dish"
        with self.assertRaisesRegex(RuntimeError, "another menu property"):
            module._assert_preserved(self.before, after)
        after = deepcopy(self.before)
        after["boms"][module.MENU["VXN-DRY-GOAT"][2]].docstatus = 2
        with self.assertRaisesRegex(RuntimeError, "protected boms"):
            module._assert_preserved(self.before, after)

    def test_current_history_uses_locking_selects_instead_of_stale_counts(self):
        module._history(for_update=True)
        self.assertEqual(self.db.sql.call_count, sum(len(fields) for fields in module.HISTORY_FIELDS.values()))
        for call in self.db.sql.call_args_list:
            self.assertIn("FOR UPDATE", call.args[0])
            self.assertNotIn("COUNT(", call.args[0])

    def test_no_intermediate_commit_capability_is_restored_after_failure(self):
        original = self.db.commit
        with self.assertRaisesRegex(RuntimeError, "intermediate commit"):
            with module._no_intermediate_commit():
                self.db.commit()
        self.assertIs(self.db.commit, original)
        original.assert_not_called()
