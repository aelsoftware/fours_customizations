"""Write-free regression coverage for the approved Chicken and Bowl recipes."""

from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from types import ModuleType
from unittest import TestCase
from unittest.mock import Mock, patch

from fours_customizations import vox_kitchen_recipes as recipes
from fours_customizations import vox_work_orders as module
from fours_customizations.tests import test_vox_work_orders as legacy


Doc = legacy.Doc
CHICKEN = "VXN-CHICKEN"
CHICKEN_BOWL = "VXN-CHICKEN-BOWL"
GOAT_BOWL = "VXN-GOAT-BOWL"
BIRD = "VXN-RAW-CHICKEN-BIRD"
IRISH = "VXN-RAW-IRISH-KG"
NEW = (CHICKEN, CHICKEN_BOWL, GOAT_BOWL)


def recipe(code):
    return {
        "bom": recipes.MENU[code], "modified": "2026-09-28 23:09:38.546262",
        "quantity": module._decimal_text(recipes.BATCHES[code]), "uom": "Pc(s)",
        "ingredients": [
            {"item_code": raw, "quantity": module._decimal_text(recipes.BATCHES[code] * per_plate),
             "uom": uom, "per_portion": module._decimal_text(per_plate)}
            for raw, uom, per_plate in recipes.COMPONENTS[code]
        ], "warehouse": module.WAREHOUSE,
    }


def plan(code=CHICKEN_BOWL, quantity=1, line="28d310b5-8c03-4ce7-9503-8e54e975d81f"):
    value = legacy.plan()
    value.update(version=2, source_item=code, production_item=code,
                 line_uuid=line, quantity=module._decimal_text(quantity), recipe=recipe(code))
    del value["raw_quantity"]
    value["ingredients"] = [
        {"item_code": raw, "quantity": module._decimal_text(Decimal(str(quantity)) * per_plate),
         "uom": uom, "warehouse": module.WAREHOUSE}
        for raw, uom, per_plate in recipes.COMPONENTS[code]
    ]
    return value


def work_order(value=None, *, completed=True):
    value = value or plan()
    old = {**value, "raw_quantity": "0.4"}
    wo = legacy.work_order(old, completed=completed)
    wo.required_items = [
        Doc(item_code=raw["item_code"], stock_uom=raw["uom"], source_warehouse=raw["warehouse"],
            required_qty=float(raw["quantity"]), consumed_qty=float(raw["quantity"]) if completed else 0,
            include_item_in_manufacturing=1)
        for raw in value["ingredients"]
    ]
    setattr(wo, module.SNAPSHOT_FIELD, module._canonical(value))
    return wo


def manufacture(value=None):
    value = value or plan()
    return Doc(
        doctype="Stock Entry", name="STE-TEST", company=module.COMPANY, work_order="MFG-WO-TEST",
        bom_no=value["recipe"]["bom"], fg_completed_qty=float(value["quantity"]),
        posting_date="2026-09-28", posting_time="23:09:40",
        items=[
            Doc(item_code=raw["item_code"], s_warehouse=module.WAREHOUSE, t_warehouse=None,
                uom=raw["uom"], stock_uom=raw["uom"], conversion_factor=1,
                qty=float(raw["quantity"]), transfer_qty=float(raw["quantity"]), is_finished_item=0)
            for raw in value["ingredients"]
        ] + [Doc(item_code=value["production_item"], s_warehouse=None, t_warehouse=module.WAREHOUSE,
                 uom="Pc(s)", stock_uom="Pc(s)", conversion_factor=1,
                 qty=float(value["quantity"]), transfer_qty=float(value["quantity"]), is_finished_item=1)],
    )


class TestVoxKitchenMultiIngredient(TestCase):
    def setUp(self):
        legacy.TestVoxWorkOrders.setUp(self)

    invoice = legacy.TestVoxWorkOrders.invoice
    source = legacy.TestVoxWorkOrders.source

    def recipe_documents(self, code):
        components = recipes.COMPONENTS[code]
        item = Doc(name=code, is_stock_item=1, disabled=0, stock_uom="Pc(s)",
                   has_serial_no=0, has_batch_no=0, has_variants=0, is_sales_item=1,
                   allow_negative_stock=0, default_bom=recipes.MENU[code])
        bom = Doc(name=recipes.MENU[code], modified="2026-09-28 23:09:38.546262", company=module.COMPANY,
                  item=code, docstatus=1, is_active=1, uom="Pc(s)", currency="UGX",
                  quantity=float(recipes.BATCHES[code]), transfer_material_against="Work Order",
                  items=[Doc(item_code=raw, uom=uom, stock_uom=uom, conversion_factor=1,
                             qty=float(per_plate * recipes.BATCHES[code]),
                             stock_qty=float(per_plate * recipes.BATCHES[code]),
                             source_warehouse=module.WAREHOUSE, include_item_in_manufacturing=1)
                         for raw, uom, per_plate in components])
        docs = {("Item", code): item, ("BOM", recipes.MENU[code]): bom}
        for raw, uom, _per_plate in components:
            docs[("Item", raw)] = Doc(name=raw, is_stock_item=1, disabled=0, stock_uom=uom,
                                      is_sales_item=0, has_serial_no=0, has_batch_no=0, has_variants=0,
                                      allow_negative_stock=0)
            docs[("UOM", uom)] = Doc(name=uom, enabled=1, must_be_whole_number=0)
        self.frappe.get_doc.side_effect = lambda doctype, name, **kwargs: docs[(doctype, name)]
        return docs

    def test_approved_mapping_contains_exact_five_dishes_and_no_shared_legacy_skus(self):
        self.assertEqual(set(recipes.MENU), {*recipes.LEGACY_MENU, *NEW})
        self.assertEqual(recipes.COMPONENTS[CHICKEN], ((BIRD, "Bird", Decimal("0.25")),))
        self.assertEqual(recipes.COMPONENTS[CHICKEN_BOWL],
                         ((BIRD, "Bird", Decimal("0.25")), (IRISH, "Kg", Decimal("0.1"))))
        self.assertEqual(recipes.COMPONENTS[GOAT_BOWL],
                         ((module.RAW, "Kg", Decimal("0.4")), (IRISH, "Kg", Decimal("0.1"))))
        self.assertEqual(recipes.BATCHES[CHICKEN], 4)
        self.assertEqual(recipes.BATCHES[CHICKEN_BOWL], 4)
        self.assertEqual(recipes.BATCHES[GOAT_BOWL], 25)
        self.assertEqual(recipes.RAW_UOMS, {BIRD: "Bird", IRISH: "Kg", module.RAW: "Kg"})
        for code in ("VX-0126", "VX-0127", "88763"):
            self.assertNotIn(code, recipes.MENU)

    def test_original_goat_recipe_serialization_stays_exact_schema_one(self):
        for code in recipes.LEGACY_MENU:
            self.recipe_documents(code)
            with self.subTest(code=code):
                self.assertEqual(module._recipe(code), legacy.recipe(code))

    def test_original_goat_plan_and_signed_receipt_production_semantics_are_unchanged(self):
        from fours_customizations import vox_modifiers

        for code in recipes.LEGACY_MENU:
            invoice, sale = self.invoice(code), self.source()
            with patch.object(vox_modifiers, "invoice_options", return_value=[]), \
                    patch.object(module, "_recipe", return_value=legacy.recipe(code)):
                value = module._plan(invoice, sale, invoice.items[0], sale.lines[0])
            self.assertEqual(value, legacy.plan(code))
            self.assertEqual(module._canonical(value), module._canonical(legacy.plan(code)))
            module._validate_work_order(legacy.work_order(value), value, completed=True)

    def test_new_recipe_snapshots_bind_native_batch_and_every_raw_unit(self):
        for code in NEW:
            self.recipe_documents(code)
            with self.subTest(code=code):
                self.assertEqual(module._recipe(code), recipe(code))
        self.assertEqual(recipe(CHICKEN_BOWL)["ingredients"], [
            {"item_code": BIRD, "quantity": "1", "uom": "Bird", "per_portion": "0.25"},
            {"item_code": IRISH, "quantity": "0.4", "uom": "Kg", "per_portion": "0.1"},
        ])
        self.assertEqual(recipe(GOAT_BOWL)["ingredients"][1]["quantity"], "2.5")

    def test_recipe_rejects_missing_extra_duplicate_and_replaced_raw_rows(self):
        for mutation in (lambda bom: bom.items.pop(),
                         lambda bom: bom.items.append(deepcopy(bom.items[0])),
                         lambda bom: setattr(bom.items[1], "item_code", BIRD),
                         lambda bom: setattr(bom.items[1], "item_code", "UNAPPROVED-IRISH")):
            docs = self.recipe_documents(CHICKEN_BOWL)
            mutation(docs[("BOM", recipes.MENU[CHICKEN_BOWL])])
            with self.assertRaises(module.ProductionError):
                module._recipe(CHICKEN_BOWL)

    def test_recipe_row_order_does_not_rewrite_canonical_frozen_order(self):
        docs = self.recipe_documents(CHICKEN_BOWL)
        docs[("BOM", recipes.MENU[CHICKEN_BOWL])].items.reverse()
        self.assertEqual(module._recipe(CHICKEN_BOWL), recipe(CHICKEN_BOWL))

    def test_recipe_rejects_changed_ingredient_quantities_units_and_conversion(self):
        for field, bad in (("qty", 0.1), ("stock_qty", 4), ("conversion_factor", 1000),
                           ("uom", "Gram"), ("stock_uom", "Nos"),
                           ("source_warehouse", "Main counter - VXN"),
                           ("include_item_in_manufacturing", 0), ("bom_no", "BOM-NESTED")):
            docs = self.recipe_documents(CHICKEN_BOWL)
            setattr(docs[("BOM", recipes.MENU[CHICKEN_BOWL])].items[1], field, bad)
            with self.subTest(field=field), self.assertRaises(module.ProductionError):
                module._recipe(CHICKEN_BOWL)

    def test_recipe_rejects_changed_bom_batch_and_process_assumptions(self):
        for field, bad in (("quantity", 1), ("company", "Vox Lounge"), ("docstatus", 0),
                           ("is_active", 0), ("with_operations", 1), ("operations", ["cook"]),
                           ("scrap_items", ["waste"]), ("secondary_items", ["stock"]),
                           ("process_loss_percentage", 5), ("process_loss_qty", 1),
                           ("transfer_material_against", "Job Card")):
            docs = self.recipe_documents(CHICKEN_BOWL)
            setattr(docs[("BOM", recipes.MENU[CHICKEN_BOWL])], field, bad)
            with self.subTest(field=field), self.assertRaises(module.ProductionError):
                module._recipe(CHICKEN_BOWL)

    def test_raw_items_must_remain_fractional_internal_stock_and_not_sale_menu(self):
        for raw in (BIRD, IRISH):
            for field, bad in (("is_stock_item", 0), ("is_sales_item", 1), ("disabled", 1),
                               ("stock_uom", "Nos"), ("has_serial_no", 1), ("has_batch_no", 1),
                               ("has_variants", 1), ("allow_negative_stock", 1),
                               ("custom_company", "Vox Lounge")):
                docs = self.recipe_documents(CHICKEN_BOWL)
                setattr(docs[("Item", raw)], field, bad)
                with self.subTest(raw=raw, field=field), self.assertRaises(module.ProductionError):
                    module._recipe(CHICKEN_BOWL)
        docs = self.recipe_documents(CHICKEN_BOWL)
        docs[("UOM", "Bird")].must_be_whole_number = 1
        with self.assertRaises(module.ProductionError):
            module._recipe(CHICKEN_BOWL)
        docs = self.recipe_documents(CHICKEN_BOWL)
        docs[("UOM", "Bird")].enabled = 0
        with self.assertRaises(module.ProductionError):
            module._recipe(CHICKEN_BOWL)

    def test_new_plans_use_schema_two_and_scale_each_recipe_without_goat_assumption(self):
        from fours_customizations import vox_modifiers

        for code in NEW:
            invoice, sale = self.invoice(code), self.source()
            invoice.items[0].qty = 4
            with patch.object(vox_modifiers, "invoice_options", return_value=[]), \
                    patch.object(module, "_recipe", return_value=recipe(code)):
                value = module._plan(invoice, sale, invoice.items[0], sale.lines[0])
            self.assertEqual(value, plan(code, quantity=4))
            self.assertNotIn("raw_quantity", value)
            self.assertEqual(value["version"], 2)
        self.assertEqual(plan(CHICKEN, 4)["ingredients"][0]["quantity"], "1")
        self.assertEqual(plan(CHICKEN_BOWL, 4)["ingredients"][1]["quantity"], "0.4")
        self.assertEqual(plan(GOAT_BOWL, 4)["ingredients"][0]["quantity"], "1.6")

    def test_chicken_options_remain_frozen_instructions_without_guessed_side_deduction(self):
        from fours_customizations import vox_modifiers

        invoice, sale = self.invoice(CHICKEN), self.source()
        setattr(invoice.items[0], module.MODIFIER_FIELD, "VKMS-CHICKEN")
        option = {"company": module.COMPANY, "item_code": CHICKEN, "label": "Chips",
                  "public_id": 123, "stock_policy": "sales_only", "selection": "VKMS-CHICKEN"}
        with patch.object(vox_modifiers, "invoice_options", return_value=[option]), \
                patch.object(module, "_recipe", return_value=recipe(CHICKEN)):
            value = module._plan(invoice, sale, invoice.items[0], sale.lines[0])
        self.assertEqual(value["instructions"], [option])
        self.assertEqual([raw["item_code"] for raw in value["ingredients"]], [BIRD])

    def test_schema_reader_accepts_only_known_exact_ingredient_vectors(self):
        expected = [{"item_code": BIRD, "quantity": Decimal("0.25"), "uom": "Bird",
                     "warehouse": module.WAREHOUSE},
                    {"item_code": IRISH, "quantity": Decimal("0.1"), "uom": "Kg",
                     "warehouse": module.WAREHOUSE}]
        self.assertEqual(module._ingredients(plan()), expected)
        for change in ({"version": 1}, {"version": True}, {"version": 3}, {"raw_quantity": "0.4"},
                       {"planning_only": True}, {"uom": "Nos"}, {"production_item": "VX-0127"},
                       {"ingredients": expected}, {"ingredients": []}):
            with self.subTest(change=change), self.assertRaises(module.ProductionError):
                module._ingredients({**plan(), **change})

    def test_legacy_schema_cannot_smuggle_new_item_or_change_goat_quantity(self):
        self.assertEqual(module._ingredients(legacy.plan())[0]["quantity"], Decimal("0.4"))
        for change in ({"production_item": CHICKEN}, {"raw_quantity": "0.25"}, {"ingredients": []}):
            with self.subTest(change=change), self.assertRaises(module.ProductionError):
                module._ingredients({**legacy.plan(), **change})

    def test_work_order_validates_every_exact_required_and_consumed_ingredient(self):
        for code in NEW:
            value = plan(code)
            module._validate_work_order(work_order(value), value, completed=True)
            module._validate_work_order(work_order(value, completed=False), value, completed=False)
        wo = work_order()
        wo.required_items.reverse()
        module._validate_work_order(wo, plan(), completed=True)

    def test_work_order_rejects_missing_duplicate_replaced_and_wrong_consumed_material(self):
        for mutation in (lambda wo: wo.required_items.pop(),
                         lambda wo: wo.required_items.append(deepcopy(wo.required_items[0])),
                         lambda wo: setattr(wo.required_items[1], "item_code", BIRD),
                         lambda wo: setattr(wo.required_items[1], "consumed_qty", 0),
                         lambda wo: setattr(wo.required_items[1], "consumed_qty", 100),
                         lambda wo: setattr(wo.required_items[1], "required_qty", 0.2),
                         lambda wo: setattr(wo.required_items[1], "stock_uom", "Gram"),
                         lambda wo: setattr(wo.required_items[1], "source_warehouse", "Bar"),
                         lambda wo: setattr(wo.required_items[1], "include_item_in_manufacturing", 0),
                         lambda wo: setattr(wo.required_items[1], "allow_alternative_item", 1)):
            wo = work_order()
            mutation(wo)
            with self.assertRaises(module.ProductionError):
                module._validate_work_order(wo, plan(), completed=True)

    def test_new_work_order_does_not_accept_schema_or_recipe_reinterpretation(self):
        original = plan()
        wo = work_order(original)
        changed = deepcopy(original)
        changed["recipe"]["modified"] = "later valuation update"
        module._validate_work_order(wo, changed, completed=True)
        changed["ingredients"][1]["quantity"] = "0.2"
        with self.assertRaises(module.ProductionError):
            module._validate_work_order(wo, changed, completed=True)

    def test_manufacture_accepts_all_exact_components_once_and_one_finished_row(self):
        for code in NEW:
            value = plan(code)
            entry = manufacture(value)
            self.db.sql.return_value = [Doc(name=entry.name, purpose="Manufacture", docstatus=1)]
            self.frappe.get_doc.return_value = entry
            self.assertIs(module._manufacture_for(work_order(value), value), entry)
            entry.items.reverse()
            self.assertIs(module._manufacture_for(work_order(value), value), entry)

    def test_manufacture_rejects_missing_or_incorrect_irish_even_with_correct_chicken(self):
        for mutation in (lambda entry: entry.items.pop(1),
                         lambda entry: entry.items.append(deepcopy(entry.items[1])),
                         lambda entry: setattr(entry.items[1], "item_code", BIRD),
                         lambda entry: setattr(entry.items[1], "transfer_qty", 100),
                         lambda entry: setattr(entry.items[1], "qty", 0.2),
                         lambda entry: setattr(entry.items[1], "stock_uom", "Gram"),
                         lambda entry: setattr(entry.items[1], "uom", "Gram"),
                         lambda entry: setattr(entry.items[1], "conversion_factor", 0.001),
                         lambda entry: setattr(entry.items[1], "s_warehouse", "Bar"),
                         lambda entry: setattr(entry.items[1], "t_warehouse", module.WAREHOUSE),
                         lambda entry: setattr(entry.items[1], "is_finished_item", 1),
                         lambda entry: setattr(entry.items[-1], "uom", "Kg")):
            entry = manufacture()
            mutation(entry)
            self.db.sql.return_value = [Doc(name=entry.name, purpose="Manufacture", docstatus=1)]
            self.frappe.get_doc.return_value = entry
            with self.assertRaises(module.ProductionError):
                module._manufacture_for(work_order(), plan())

    def test_inventory_lock_includes_all_three_raws_five_finished_and_boms_in_stable_order(self):
        module._lock_inventory()
        calls = self.db.sql.call_args_list
        expected = tuple(sorted((*recipes.RAW_UOMS, *recipes.MENU, module.PORTION)))
        self.assertEqual(calls[0].args[1], (expected,))
        self.assertEqual(calls[1].args[1], (expected, module.WAREHOUSE))
        self.assertEqual(calls[2].args[1], (tuple(recipes.MENU.values()),))
        self.assertTrue(all("FOR UPDATE" in call.args[0] for call in calls))

    def _engine(self, plans, *, available=None, existing=None):
        invoice = self.invoice(plans[0]["production_item"])
        invoice.items = [Doc(name=f"SII-{index}", item_code=value["production_item"], qty=float(value["quantity"]),
                             rate=15000, uom="Pc(s)", stock_uom="Pc(s)", conversion_factor=1,
                             warehouse=module.WAREHOUSE) for index, value in enumerate(plans)]
        sale = self.source()
        pairs = [(row, Doc(line_id=index + 1, desktop_line_uuid=value["line_uuid"]))
                 for index, (row, value) in enumerate(zip(invoice.items, plans, strict=True))]
        for name, value in (("_require_schema", None), ("_trusted_source", (sale, {})),
                            ("_lock_inventory", None), ("_matched_lines", pairs),
                            ("_validate_work_order", None), ("_manufacture_for", None), ("_assert_lineage", None),
                            ("_settle_raw_bin_values", [])):
            self.enterContext(patch.object(module, name, return_value=value))
        self.enterContext(patch.object(module, "_plan", side_effect=plans))
        self.enterContext(patch.object(module, "_existing", side_effect=existing or [None] * len(plans)))
        self.enterContext(patch.object(module, "_actual", return_value=Decimal(0)))
        stock = self.enterContext(patch.object(module, "_available", side_effect=available or (lambda *a, **k: Decimal(1000))))
        new = self.enterContext(patch.object(module, "_new_work_order", side_effect=lambda _invoice, value, _context: work_order(value)
                                           if value["version"] == 2 else legacy.work_order(value)))
        complete = self.enterContext(patch.object(module, "_complete_work_order", side_effect=lambda wo, _value, _context: wo))
        return invoice, stock, new, complete

    def test_cart_aggregates_shared_bird_goat_and_irish_before_any_work_order(self):
        plans = [plan(CHICKEN, quantity=3),
                 plan(CHICKEN_BOWL, quantity=2, line="18d310b5-8c03-4ce7-9503-8e54e975d81f"),
                 plan(GOAT_BOWL, quantity=4, line="38d310b5-8c03-4ce7-9503-8e54e975d81f"),
                 legacy.plan(line="48d310b5-8c03-4ce7-9503-8e54e975d81f")]
        available = {BIRD: Decimal("1.25"), module.RAW: Decimal("2"), IRISH: Decimal("0.6")}
        calls_before_cooking = []

        def stock(code, when, **kwargs):
            if kwargs.get("future_floor"):
                calls_before_cooking.append(code)
                new.assert_not_called()
                return available[code]
            return Decimal(1000)

        invoice, _stock, new, complete = self._engine(plans, available=stock)
        self.assertEqual(len(module.ensure_invoice_production(invoice)), 4)
        self.assertEqual(calls_before_cooking, sorted(available))
        self.assertEqual(new.call_count, 4)
        self.assertEqual(complete.call_count, 4)
        self.db.commit.assert_not_called()

    def test_shared_irish_shortage_blocks_entire_cart_before_any_chicken_or_goat_is_cooked(self):
        plans = [plan(CHICKEN_BOWL), plan(GOAT_BOWL, line="18d310b5-8c03-4ce7-9503-8e54e975d81f")]
        invoice, _stock, new, complete = self._engine(
            plans, available=lambda code, *a, **k: Decimal("0.15") if code == IRISH else Decimal(10))
        with self.assertRaisesRegex(module.ProductionError, "Kitchen stock is insufficient"):
            module.ensure_invoice_production(invoice)
        new.assert_not_called()
        complete.assert_not_called()
        self.db.commit.assert_not_called()

    def test_shared_bird_shortage_is_not_checked_per_plate_in_isolation(self):
        plans = [plan(CHICKEN, 3), plan(CHICKEN_BOWL, 2, line="18d310b5-8c03-4ce7-9503-8e54e975d81f")]
        invoice, _stock, new, complete = self._engine(
            plans, available=lambda code, *a, **k: Decimal(1) if code == BIRD else Decimal(10))
        with self.assertRaisesRegex(module.ProductionError, "Kitchen stock is insufficient"):
            module.ensure_invoice_production(invoice)
        new.assert_not_called()
        complete.assert_not_called()

    def test_existing_bowl_amendment_reuses_food_without_raw_deduction(self):
        value = plan()
        invoice, available, new, complete = self._engine([value], existing=[work_order(value)])
        with patch.object(module, "_actual", return_value=Decimal(1)), \
                patch.object(module, "_settle_raw_bin_values") as settle:
            self.assertEqual(module.ensure_invoice_production(invoice), ["MFG-WO-TEST"])
        self.assertEqual([call.args[0] for call in available.call_args_list], [CHICKEN_BOWL])
        new.assert_not_called()
        complete.assert_not_called()
        settle.assert_not_called()

    def test_raw_value_settlement_runs_after_every_new_manufacture_before_returning_to_invoice(self):
        plans = [plan(CHICKEN), plan(CHICKEN_BOWL, line="18d310b5-8c03-4ce7-9503-8e54e975d81f")]
        invoice, _stock, new, complete = self._engine(plans)

        def settle(made, context):
            self.assertEqual(new.call_count, 2)
            self.assertEqual(complete.call_count, 2)
            self.assertEqual([value for value, _wo in made], plans)
            self.assertIs(module._WRITING.get(), context)
            self.assertEqual(invoice.docstatus, 0)

        with patch.object(module, "_settle_raw_bin_values", side_effect=settle) as settlement:
            module.ensure_invoice_production(invoice)
        settlement.assert_called_once()
        self.db.commit.assert_not_called()

    def test_completion_checks_each_ingredient_excluding_only_its_own_native_reservation(self):
        native = ModuleType("erpnext.manufacturing.doctype.work_order.work_order")
        native.make_stock_entry = make = Mock(return_value={})
        value, wo = plan(), work_order(completed=False)
        entry = manufacture()
        entry.insert = Mock()
        entry.submit = Mock()
        self.frappe.get_doc.side_effect = [entry, work_order()]
        with patch.object(module, "_available", return_value=Decimal(10)) as available, \
                patch.dict("sys.modules", {native.__name__: native}), \
                patch.object(module, "_validate_work_order") as validate, \
                patch.object(module, "_manufacture_for") as evidence, module._native_writes() as context:
            module._complete_work_order(wo, value, context)
        self.assertEqual([call.args[0] for call in available.call_args_list], [BIRD, IRISH])
        self.assertTrue(all(call.kwargs == {"future_floor": True, "exclude_work_order": wo.name}
                            for call in available.call_args_list))
        make.assert_called_once_with(wo.name, "Manufacture", qty=1.0)
        entry.insert.assert_called_once_with(ignore_permissions=True)
        entry.submit.assert_called_once()
        validate.assert_called_once()
        evidence.assert_called_once()
        self.db.commit.assert_not_called()

    def test_completion_irish_shortage_prevents_native_stock_entry_generation(self):
        native = ModuleType("erpnext.manufacturing.doctype.work_order.work_order")
        native.make_stock_entry = make = Mock()
        with patch.object(module, "_available", side_effect=[Decimal(1), Decimal("0.05")]), \
                patch.dict("sys.modules", {native.__name__: native}), module._native_writes() as context:
            with self.assertRaisesRegex(module.ProductionError, "Kitchen stock is insufficient"):
                module._complete_work_order(work_order(completed=False), plan(), context)
        make.assert_not_called()

    def test_new_dishes_route_only_nansana_to_kitchen(self):
        for code in NEW:
            invoice = self.invoice(code)
            invoice.items[0].warehouse = "Main counter - VXN"
            self.db.get_value.return_value = Doc(company=module.COMPANY, disabled=0, is_group=0)
            module.route_invoice_warehouses(invoice)
            self.assertEqual(invoice.items[0].warehouse, module.WAREHOUSE)
            invoice.company = "Vox Lounge"
            invoice.items[0].warehouse = "Other kitchen"
            module.route_invoice_warehouses(invoice)
            self.assertEqual(invoice.items[0].warehouse, "Other kitchen")

    def test_new_dish_finish_replay_can_only_refresh_derived_dish_rate_not_raw_rates(self):
        value = {"doctype": "Sales Invoice Item", "item_code": CHICKEN_BOWL,
                 "warehouse": module.WAREHOUSE, "incoming_rate": 7500, "rate": 20000}
        self.assertNotIn("incoming_rate", module._valuation_frozen(value))
        for raw in (BIRD, IRISH):
            self.assertIn("incoming_rate", module._valuation_frozen({**value, "item_code": raw}))

    def test_finished_valuation_snapshot_freezes_all_shared_raw_bins(self):
        sle = Doc(voucher_type="Sales Invoice", voucher_no="SI-NEW")
        self.db.sql.side_effect = [[sle], [], [], []]
        self.frappe.get_doc.return_value = Doc(docstatus=1, company=module.COMPANY)
        state = module._valuation_snapshot((CHICKEN_BOWL,), datetime(2026, 9, 28, 23, 9, 40))
        self.assertEqual(state["bins"], [])
        last = self.db.sql.call_args_list[-1]
        self.assertEqual(last.args[1][0], tuple(sorted((*recipes.RAW_UOMS, *recipes.MENU, module.PORTION))))
        self.assertIn("FOR UPDATE", last.args[0])
