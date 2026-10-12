"""Standalone, write-free tests of the bounded Vox native production contract."""

from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from fours_customizations import vox_work_orders as module


class Doc(SimpleNamespace):
    def get(self, name, default=None):
        return self.__dict__.get(name, default)

    def as_dict(self):
        return deepcopy(self.__dict__)


def recipe(code="VXN-DRY-GOAT"):
    return {"bom": module.MENU[code], "modified": "2026-09-28 23:09:38.546262",
            "quantity": "25", "uom": "Pc(s)", "raw_item": module.RAW,
            "raw_qty": "10", "raw_uom": "Kg", "raw_per_portion": "0.4", "warehouse": module.WAREHOUSE}


def plan(code="VXN-DRY-GOAT", line="28d310b5-8c03-4ce7-9503-8e54e975d81f"):
    return {"version": 1, "company": module.COMPANY, "sale": "4929",
            "sale_label": "KOD-2-TEST",
            "sale_uuid": module.PILOT["sale_uuid"], "line_id": "15806", "line_uuid": line,
            "source_item": code, "production_item": code, "quantity": "1", "uom": "Pc(s)",
            "raw_quantity": "0.4", "posting_datetime": "2026-09-28 23:09:40",
            "recipe": recipe(code), "modifier_reference": "", "instructions": [], "planning_only": False}


def work_order(snapshot=None, *, completed=True):
    snapshot = snapshot or plan()
    return Doc(doctype="Work Order", name="MFG-WO-TEST", company=module.COMPANY, production_item=snapshot["production_item"],
               bom_no=snapshot["recipe"]["bom"], docstatus=1, qty=float(snapshot["quantity"]),
               stock_uom=snapshot["uom"], source_warehouse=module.WAREHOUSE, fg_warehouse=module.WAREHOUSE,
               skip_transfer=1, from_wip_warehouse=0, wip_warehouse=None, operations=[],
               process_loss_qty=0, disassembled_qty=0, status="Completed" if completed else "Not Started",
               produced_qty=float(snapshot["quantity"]) if completed else 0,
               required_items=[Doc(item_code=module.RAW, stock_uom="Kg", source_warehouse=module.WAREHOUSE,
                                   required_qty=float(snapshot["raw_quantity"]),
                                   consumed_qty=float(snapshot["raw_quantity"]) if completed else 0)],
               **{module.KEY_FIELD: module.production_key(snapshot["sale_uuid"], snapshot["line_uuid"]),
                  module.SNAPSHOT_FIELD: module._canonical(snapshot), module.INVOICE_FIELD: "SI-ORIGINAL"})


class TestVoxWorkOrders(TestCase):
    def setUp(self):
        self.db = SimpleNamespace(
            sql=Mock(return_value=[]), get_value=Mock(return_value=0),
            get_single_value=Mock(side_effect=lambda doctype, field: 1 if field == "allow_negative_stock" else 0),
            commit=Mock(), auto_commit_on_many_writes=False, _disable_transaction_control=False,
            has_column=Mock(return_value=True), exists=Mock(return_value=False), transaction_writes=0,
        )
        self.frappe = SimpleNamespace(
            db=self.db, local=SimpleNamespace(site=module.SITE), session=SimpleNamespace(user="cashier@example.com"),
            conf={module.CONFIG: {module.COMPANY: True}}, get_all=Mock(return_value=[]), get_doc=Mock(),
        )
        self.enterContext(patch.object(module, "frappe", self.frappe))
        self.enterContext(patch.object(module, "now_datetime", return_value=datetime(2026, 9, 29, 1)))

    def invoice(self, code="VXN-DRY-GOAT"):
        return Doc(doctype="Sales Invoice", name="SI-NEW", company=module.COMPANY, docstatus=0,
                   customer="Vox Nansana Walk-in", currency="UGX", conversion_rate=1,
                   is_pos=1, is_created_using_pos=1, update_stock=1, is_return=0,
                   pos_profile="Main Bar", set_warehouse="Main counter - VXN",
                   posting_date="2026-09-28", posting_time="23:09:40", grand_total=15000,
                   items=[Doc(name="SII-NEW-1", item_code=code, qty=1, rate=15000, uom="Pc(s)", stock_uom="Pc(s)",
                              conversion_factor=1, warehouse=module.WAREHOUSE)])

    def source(self):
        return Doc(name=4929, invoice_no="KOD-2-TEST", sale_uuid=module.PILOT["sale_uuid"], sales_invoice="SI-NEW", status="final",
                   is_suspend=0, is_quotation=0, payment_status="paid", final_total=15000, amount_paid=15000,
                   payments=[Doc(amount=15000, is_return=0)], transaction_date="2026-09-28 23:09:40",
                   lines=[Doc(line_id=15806, desktop_line_uuid=module.PILOT["line_uuid"])] )

    def test_config_is_exactly_scoped_and_off_by_default(self):
        self.assertTrue(module._enabled(module.COMPANY))
        self.frappe.conf = {}
        self.assertFalse(module._enabled(module.COMPANY))
        self.frappe.conf = {module.CONFIG: "invalid"}
        self.assertFalse(module._enabled("Vox Lounge"))
        with self.assertRaises(module.ProductionError):
            module._enabled(module.COMPANY)
        self.frappe.conf = {module.CONFIG: {module.COMPANY: "true"}}
        with self.assertRaises(module.ProductionError):
            module._enabled(module.COMPANY)
        self.frappe.local.site = "ddamu.kit.africa"
        self.assertFalse(module._enabled(module.COMPANY))

    def test_routing_changes_only_dedicated_rows_not_bar_header(self):
        invoice = self.invoice()
        drink = Doc(item_code="VX-0012", warehouse="Main counter - VXN")
        legacy = Doc(item_code="VX-0128", warehouse="Main counter - VXN")
        invoice.items.extend([drink, legacy])
        self.db.get_value.return_value = Doc(company=module.COMPANY, disabled=0, is_group=0)
        module.route_invoice_warehouses(invoice)
        self.assertEqual(invoice.items[0].warehouse, module.WAREHOUSE)
        self.assertEqual(invoice.set_warehouse, "Main counter - VXN")
        self.assertEqual(drink.warehouse, "Main counter - VXN")
        self.assertEqual(legacy.warehouse, "Main counter - VXN")
        invoice.company = "4S Industries Limited"
        invoice.items[0].warehouse = "Other Warehouse"
        module.route_invoice_warehouses(invoice)
        self.assertEqual(invoice.items[0].warehouse, "Other Warehouse")

    def test_stable_key_does_not_depend_on_invoice_or_price(self):
        key = module.production_key(module.PILOT["sale_uuid"], module.PILOT["line_uuid"])
        self.assertTrue(key.startswith(module.KEY_PREFIX))
        self.assertEqual(key, module.production_key(module.PILOT["sale_uuid"], module.PILOT["line_uuid"]))
        self.assertNotEqual(key, module.production_key(module.PILOT["sale_uuid"], "18d310b5-8c03-4ce7-9503-8e54e975d81f"))
        with self.assertRaises(module.ProductionError):
            module.production_key("forged", module.PILOT["line_uuid"])

    def test_numbers_reject_nonfinite_negative_bool_and_missing(self):
        for value in (None, "", True, "NaN", "Infinity", -1, 0):
            with self.subTest(value=value), self.assertRaises(module.ProductionError):
                module._number(value)
        self.assertEqual(module._number("0.4"), Decimal("0.4"))
        self.assertEqual(module._number(0, zero=True), 0)

    def test_cutover_is_historical_not_current_posting_substitution(self):
        invoice = self.invoice()
        self.assertEqual(module._posted_at(invoice), datetime(2026, 9, 28, 23, 9, 40))
        invoice.posting_time = "17:59:59"
        with self.assertRaises(module.ProductionError):
            module._posted_at(invoice)
        invoice.posting_date = "2026-10-01"
        with self.assertRaises(module.ProductionError):
            module._posted_at(invoice)

    def test_whole_projection_rejects_reordering_conversion_and_stripped_options(self):
        from kit_pos.connector import materialize
        invoice, sale = self.invoice(), self.source()
        projected = [{"item_code": "VXN-DRY-GOAT", "qty": 1, "rate": 15000,
                      "uom": "Pc(s)", "conversion_factor": 1}]
        with patch.object(materialize, "_invoice_items", return_value=projected):
            self.assertEqual(module._matched_lines(invoice, sale, {}), [(invoice.items[0], sale.lines[0])])
            for field, invalid in (("item_code", "VXN-PAN-FRIED-GOAT"), ("qty", 2), ("rate", 1),
                                   ("uom", "Kg"), ("conversion_factor", 10)):
                original = getattr(invoice.items[0], field)
                setattr(invoice.items[0], field, invalid)
                with self.subTest(field=field), self.assertRaises(module.ProductionError):
                    module._matched_lines(invoice, sale, {})
                setattr(invoice.items[0], field, original)
            projected[0][module.MODIFIER_FIELD] = "VKMS-frozen"
            with self.assertRaises(module.ProductionError):
                module._matched_lines(invoice, sale, {})

    def test_partial_portions_and_duplicate_source_uuid_are_rejected(self):
        from kit_pos.connector import materialize
        invoice, sale = self.invoice(), self.source()
        projected = [{"item_code": "VXN-DRY-GOAT", "qty": 0.5, "rate": 15000,
                      "uom": "Pc(s)", "conversion_factor": 1}]
        invoice.items[0].qty = 0.5
        with patch.object(materialize, "_invoice_items", return_value=projected):
            with self.assertRaises(module.ProductionError):
                module._matched_lines(invoice, sale, {})
            invoice.items[0].qty = projected[0]["qty"] = 1
            invoice.items.append(deepcopy(invoice.items[0]))
            projected.append(deepcopy(projected[0]))
            sale.lines.append(Doc(line_id=15807, desktop_line_uuid=module.PILOT["line_uuid"]))
            with self.assertRaises(module.ProductionError):
                module._matched_lines(invoice, sale, {})

    def test_manifest_uses_frozen_sales_only_options_and_ignores_invoice_name(self):
        from fours_customizations import vox_modifiers
        invoice, sale = self.invoice(), self.source()
        option = {"company": module.COMPANY, "item_code": "VXN-DRY-GOAT", "label": "Rice",
                  "group_name": "Sides", "price": "10000", "currency": "UGX", "stock_policy": "sales_only",
                  "public_id": 123, "selection": "VKMS-frozen", "version": 1}
        setattr(invoice.items[0], module.MODIFIER_FIELD, "VKMS-frozen")
        with patch.object(vox_modifiers, "invoice_options", return_value=[option]), patch.object(module, "_recipe", return_value=recipe()):
            first = module._plan(invoice, sale, invoice.items[0], sale.lines[0])
            invoice.name = "SI-PAYMENT-AMENDMENT"
            second = module._plan(invoice, sale, invoice.items[0], sale.lines[0])
            self.assertEqual(first, second)
            self.assertEqual(first["raw_quantity"], "0.4")
            self.assertEqual(first["instructions"], [option])
            option["stock_policy"] = "consume_guessed_portion"
            with self.assertRaises(module.ProductionError):
                module._plan(invoice, sale, invoice.items[0], sale.lines[0])

    def test_existing_work_order_requires_exact_original_physical_instructions(self):
        expected = plan()
        wo = work_order(expected)
        module._validate_work_order(wo, expected, completed=True)
        for change in ({"quantity": "2"}, {"source_item": "VXN-PAN-FRIED-GOAT"},
                       {"instructions": [{"label": "Chips"}]}, {"recipe": {**recipe(), "raw_qty": "20"}}):
            changed = {**expected, **change}
            with self.subTest(change=change), self.assertRaises(module.ProductionError):
                module._validate_work_order(wo, changed, completed=True)
        module._validate_work_order(wo, {**expected, "recipe": {**recipe(), "modified": "later"}}, completed=True)
        wo.required_items[0].consumed_qty = 0.8
        with self.assertRaises(module.ProductionError):
            module._validate_work_order(wo, expected, completed=True)

    def test_planned_work_order_is_not_claimed_as_completed(self):
        wo = work_order(completed=False)
        module._validate_work_order(wo, plan(), completed=False)
        with self.assertRaises(module.ProductionError):
            module._validate_work_order(wo, plan(), completed=True)

    def test_available_respects_historical_current_future_and_external_holds(self):
        from fours_customizations import vox_catalogue_stock
        self.db.sql.side_effect = [[(2,)], [(Decimal("1.2"),)]]
        with patch.object(module, "_stock_at", return_value=Decimal("1.6")), patch.object(vox_catalogue_stock, "reservation_hold", return_value=Decimal("0.4")) as hold:
            available = module._available(module.RAW, datetime(2026, 9, 28, 23), future_floor=True, exclude_work_order="OWN-WO")
        self.assertEqual(available, Decimal("0.8"))
        self.assertTrue(all("FOR UPDATE" in call.args[0] for call in self.db.sql.call_args_list))
        hold.assert_called_once_with(module.RAW, module.WAREHOUSE, exclude_work_order="OWN-WO", for_update=True)

    def test_generated_permission_capability_is_object_bound_and_restores_commit(self):
        doc = work_order()
        commit = self.db.commit
        with self.assertRaises(module.ProductionError):
            module.guard_generated_document(doc, "before_cancel")
        with module._native_writes() as context:
            context.objects[id(doc)] = doc
            module.guard_generated_document(doc, "before_submit")
            with self.assertRaises(module.ProductionError):
                module.guard_generated_document(deepcopy(doc), "before_submit")
            with self.assertRaises(module.ProductionError):
                self.db.commit()
        self.assertIs(self.db.commit, commit)
        commit.assert_not_called()
        with self.assertRaises(module.ProductionError):
            module.guard_generated_document(doc, "on_trash")

    def test_native_write_context_preserves_framework_hook_nesting(self):
        commit = self.db.commit
        for depth in (0, 1, 2, 7):
            self.db._disable_transaction_control = depth
            with self.subTest(depth=depth), module._native_writes() as context:
                self.assertEqual(self.db._disable_transaction_control, depth)
                doc = work_order()
                context.objects[id(doc)] = doc
                self.db._disable_transaction_control += 1
                try:
                    module.guard_generated_document(doc, "before_submit")
                    self.assertEqual(self.db._disable_transaction_control, depth + 1)
                    with self.assertRaises(module.ProductionError):
                        self.db.commit()
                finally:
                    self.db._disable_transaction_control -= 1
            self.assertEqual(self.db._disable_transaction_control, depth)
            self.assertIs(self.db.commit, commit)
        commit.assert_not_called()

    def test_native_write_exception_does_not_reset_framework_counter(self):
        commit = self.db.commit
        self.db._disable_transaction_control = 3
        with self.assertRaisesRegex(ValueError, "native failure"):
            with module._native_writes():
                raise ValueError("native failure")
        self.assertEqual(self.db._disable_transaction_control, 3)
        self.assertIs(self.db.commit, commit)
        self.assertIsNone(module._WRITING.get())

    def test_native_write_context_still_rejects_auto_commit_inside_hook(self):
        commit = self.db.commit
        self.db._disable_transaction_control = 1
        self.db.auto_commit_on_many_writes = True
        with self.assertRaises(module.ProductionError):
            with module._native_writes():
                self.fail("Auto-commit must not be allowed.")
        self.assertEqual(self.db._disable_transaction_control, 1)
        self.assertIs(self.db.commit, commit)
        commit.assert_not_called()

    def test_real_frappe_document_hook_runner_can_enter_production_context(self):
        from frappe.model import document

        invoice = self.invoice()
        self.db._disable_transaction_control = 0

        def before_submit(doc):
            self.assertIs(doc, invoice)

        def app_hook(doc, method):
            self.assertIs(doc, invoice)
            self.assertEqual(method, "before_submit")
            self.assertEqual(self.db._disable_transaction_control, 1)
            with module._native_writes():
                self.assertEqual(self.db._disable_transaction_control, 1)
                with self.assertRaises(module.ProductionError):
                    self.db.commit()
            self.assertEqual(self.db._disable_transaction_control, 1)

        self.frappe.get_doc_hooks = Mock(return_value={"Sales Invoice": {"before_submit": ["test_hook"]}})
        self.frappe.get_attr = Mock(return_value=app_hook)
        with patch.object(document, "frappe", self.frappe):
            document.Document.hook(before_submit)(invoice)
        self.assertEqual(self.db._disable_transaction_control, 0)
        self.db.commit.assert_not_called()

    def test_guards_detect_clearing_a_persisted_work_order_key(self):
        self.db.sql.return_value = [("VXWO-existing",)]
        with self.assertRaises(module.ProductionError):
            module.guard_generated_document(Doc(doctype="Work Order", name="EXISTING"), "validate")

    def test_guards_detect_clearing_a_stock_entry_work_order(self):
        self.db.sql.side_effect = [[("MFG-WO-EXISTING",)], [("VXWO-existing",)]]
        with self.assertRaises(module.ProductionError):
            module.guard_generated_document(Doc(doctype="Stock Entry", name="STE-EXISTING", work_order=None), "validate")

    def test_cancelling_physical_documents_is_forbidden_even_inside_capability(self):
        doc = work_order()
        with module._native_writes() as context:
            context.objects[id(doc)] = doc
            for method in ("before_cancel", "on_trash"):
                with self.subTest(method=method), self.assertRaises(module.ProductionError):
                    module.guard_generated_document(doc, method)

    def test_capability_keeps_real_object_and_only_generated_native_permissions(self):
        doc = work_order(completed=False)
        doc.get_items_and_operations_from_bom = Mock()
        doc.insert = Mock()
        doc.submit = Mock()
        self.frappe.get_doc.return_value = doc
        with module._native_writes() as context:
            self.assertIs(module._new_work_order(self.invoice(), plan(), context), doc)
            self.assertIs(context.objects[id(doc)], doc)
        doc.insert.assert_called_once_with(ignore_permissions=True)
        doc.submit.assert_called_once_with()
        self.assertEqual(self.frappe.session.user, "cashier@example.com")
        self.assertFalse(hasattr(self.frappe, "flags"))

    def test_native_private_error_details_are_sanitized_and_retryable_errors_survive(self):
        self.frappe.local.message_log = ["earlier message"]
        with self.assertRaisesRegex(module.ProductionError, "configuration"):
            with module._public_failure(module.UNAVAILABLE):
                self.frappe.local.message_log.append("private raw cost and quantity")
                raise ValueError("raw cost and quantity")
        self.assertEqual(self.frappe.local.message_log, ["earlier message"])

        class RetryError(Exception):
            pass

        retry = RetryError("deadlock")
        with patch.object(module, "_RETRYABLE", (RetryError,)):
            with self.assertRaises(RetryError) as raised:
                with module._public_failure(module.UNAVAILABLE):
                    raise retry
        self.assertIs(raised.exception, retry)

    def test_current_stock_reads_never_use_repeatable_read_snapshot(self):
        self.db.get_value.return_value = 50  # the stale snapshot must be ignored
        self.db.sql.return_value = [(Decimal("0.2"),)]
        self.assertEqual(module._actual(module.RAW), Decimal("0.2"))
        self.assertEqual(module._stock_at(module.RAW, datetime(2026, 9, 28, 23)), Decimal("0.2"))
        self.db.get_value.assert_not_called()
        self.assertTrue(all("FOR UPDATE" in call.args[0] for call in self.db.sql.call_args_list))

    def _source_context(self, *, context=True, linked=True):
        from kit_pos.connector import materialize
        invoice, sale = self.invoice(), self.source()
        location = Doc(name="Main Bar", company=module.COMPANY, warehouse="Main counter - VXN",
                       pos_profile="Main Bar", selling_price_list="Selling Price Vox Nansana")
        capability = Doc(invoice=invoice, sale=sale, location=location, user=self.frappe.session.user) if context else None
        self.frappe.get_all.return_value = [Doc(name=4929)] if linked else []
        self.enterContext(patch.object(materialize, "_invoice_submission_context", return_value=capability))
        self.enterContext(patch.object(materialize, "_locked_sale", return_value=sale))
        self.enterContext(patch.object(materialize, "_reject_finalized_opening"))
        self.enterContext(patch.object(materialize, "_location_for", return_value=location))
        self.enterContext(patch.object(materialize, "_customer_for", return_value=invoice.customer))
        return invoice, sale, location

    def test_verified_context_and_existing_link_are_valid_provenance(self):
        invoice, sale, location = self._source_context(context=True, linked=False)
        self.assertEqual(module._trusted_source(invoice), (sale, location))

    def test_forged_flags_without_link_or_capability_do_not_authorize(self):
        invoice, _sale, _location = self._source_context(context=False, linked=False)
        invoice.flags = Doc(kit_pos_sale_id=4929, ignore_permissions=True)
        with self.assertRaises(module.ProductionError):
            module._trusted_source(invoice)

    def test_unpaid_held_voided_and_wrong_company_orders_cannot_cook(self):
        invoice, sale, _location = self._source_context()
        for field, invalid in (("is_suspend", 1), ("payment_status", "due"), ("amount_paid", 0),
                               ("is_quotation", 1), ("deleted_at", "2026-09-28"), ("voided_bill", "VOID-1")):
            prior = sale.get(field)
            setattr(sale, field, invalid)
            with self.subTest(field=field), self.assertRaises(module.ProductionError):
                module._trusted_source(invoice)
            setattr(sale, field, prior)
        invoice.company = "Vox Lounge"
        with self.assertRaises(module.ProductionError):
            module._trusted_source(invoice)

    def test_origin_conflicting_link_is_rejected(self):
        invoice, _sale, _location = self._source_context()
        self.frappe.get_all.return_value = [Doc(name=9999)]
        with self.assertRaises(module.ProductionError):
            module._trusted_source(invoice)

    def test_cash_given_includes_change_without_treating_sale_as_unpaid(self):
        invoice, sale, location = self._source_context()
        invoice.grand_total = sale.final_total = 30000
        sale.amount_paid = 50000
        sale.payments = [Doc(amount=50000, is_return=0), Doc(amount=20000, is_return=1)]
        self.assertEqual(module._trusted_source(invoice), (sale, location))

    def test_wrong_customer_currency_and_conversion_are_not_source_equivalent(self):
        invoice, _sale, _location = self._source_context()
        for fieldname, value in (("customer", "Another customer"), ("currency", "USD"), ("conversion_rate", 3500)):
            prior = invoice.get(fieldname)
            setattr(invoice, fieldname, value)
            with self.subTest(field=fieldname), self.assertRaises(module.ProductionError):
                module._trusted_source(invoice)
            setattr(invoice, fieldname, prior)

    def test_cross_invoice_capability_requires_explicit_cancelled_current_ancestor(self):
        invoice, sale, location = self._source_context(linked=False)
        sale.sales_invoice = "SI-ORIGINAL"
        original = deepcopy(invoice)
        original.name = "SI-ORIGINAL"
        original.docstatus = 2
        self.frappe.get_doc.return_value = original
        with self.assertRaises(module.ProductionError):
            module._trusted_source(invoice)
        invoice.amended_from = "SI-ORIGINAL"
        self.assertEqual(module._trusted_source(invoice), (sale, location))
        original.docstatus = 1
        with self.assertRaises(module.ProductionError):
            module._trusted_source(invoice)

    def test_stripped_all_menu_rows_still_enters_source_verification(self):
        invoice = self.invoice(code="VX-0012")
        with patch.object(module, "_source_has_menu", return_value=True), patch.object(module, "_ensure_invoice_production", side_effect=module.ProductionError(module.UNVERIFIED)) as ensure:
            with self.assertRaises(module.ProductionError):
                module.ensure_invoice_production(invoice)
        ensure.assert_called_once_with(invoice)

    def test_source_menu_detection_uses_core_ids_when_denormalized_item_is_absent(self):
        from kit_pos.connector import ids
        from kit_pos.connector import materialize
        invoice, sale = self.invoice(code="VX-0012"), self.source()
        sale.lines[0].product_id = sale.lines[0].variation_id = 999
        with patch.object(module, "_source_identity", return_value=(None, "4929")), patch.object(materialize, "_locked_sale", return_value=sale), patch.object(ids, "lookup", return_value="VXN-DRY-GOAT"):
            self.assertTrue(module._source_has_menu(invoice))

    def test_active_other_invoice_cannot_reuse_physical_key(self):
        invoice = self.invoice()
        self.db.sql.return_value = [("SI-OTHER-ACTIVE",)]
        with self.assertRaisesRegex(module.ProductionError, "not be cooked twice"):
            module._assert_lineage(invoice, invoice.items[0], plan(), work_order())

    def test_third_party_work_order_reference_is_rejected(self):
        invoice = self.invoice()
        invoice.name = "SI-ORIGINAL"
        setattr(invoice.items[0], module.WORK_ORDER_FIELD, "THIRD-PARTY-WO")
        with self.assertRaises(module.ProductionError):
            module._assert_lineage(invoice, invoice.items[0], plan(), work_order())

    def test_bare_copy_without_cancelled_ancestor_cannot_reuse(self):
        invoice = self.invoice()
        with self.assertRaises(module.ProductionError):
            module._assert_lineage(invoice, invoice.items[0], plan(), work_order())

    def test_submitted_trace_survives_billing_cancellation_but_cannot_be_stripped(self):
        invoice = self.invoice()
        invoice.docstatus = 1
        setattr(invoice.items[0], module.KEY_FIELD, module.production_key(plan()["sale_uuid"], plan()["line_uuid"]))
        setattr(invoice.items[0], module.WORK_ORDER_FIELD, "MFG-WO-TEST")
        original = deepcopy(invoice)
        self.db.sql.return_value = [(1,)]
        self.frappe.get_doc.return_value = original
        invoice.docstatus = 2
        module.guard_invoice_trace(invoice, "before_cancel")
        setattr(invoice.items[0], module.KEY_FIELD, None)
        setattr(invoice.items[0], module.WORK_ORDER_FIELD, None)
        with self.assertRaises(module.ProductionError):
            module.guard_invoice_trace(invoice, "before_update_after_submit")

    def test_manufacture_must_have_exact_original_posting_stamp_and_physical_quantities(self):
        wo = work_order()
        entry = Doc(doctype="Stock Entry", name="STE-TEST", company=module.COMPANY, work_order=wo.name,
                    bom_no=plan()["recipe"]["bom"], fg_completed_qty=1,
                    posting_date="2026-09-28", posting_time="23:09:40",
                    items=[Doc(item_code=module.RAW, s_warehouse=module.WAREHOUSE, t_warehouse=None,
                               transfer_qty=0.4, is_finished_item=0),
                           Doc(item_code="VXN-DRY-GOAT", s_warehouse=None, t_warehouse=module.WAREHOUSE,
                               transfer_qty=1, is_finished_item=1)])
        self.db.sql.return_value = [Doc(name=entry.name, purpose="Manufacture", docstatus=1)]
        self.frappe.get_doc.return_value = entry
        self.assertIs(module._manufacture_for(wo, plan()), entry)
        self.frappe.get_doc.assert_called_with("Stock Entry", "STE-TEST", for_update=True)
        entry.posting_time = "23:09:41"
        with self.assertRaises(module.ProductionError):
            module._manufacture_for(wo, plan())
        entry.posting_time = "23:09:40"
        entry.items[0].transfer_qty = 0.8
        with self.assertRaises(module.ProductionError):
            module._manufacture_for(wo, plan())

    def test_exact_held_pilot_uses_both_registry_ids_without_denormalized_item_field(self):
        from kit_pos.connector import ids
        invoice, sale = self.invoice(code=module.PILOT["item_code"]), self.source()
        invoice.name = sale.sales_invoice = module.PILOT["invoice"]
        invoice.grand_total = sale.final_total = 49000
        invoice.paid_amount = sale.amount_paid = 0
        invoice.items[0].rate = 25000
        sale.invoice_no = "KOD-2-101"
        sale.is_suspend = 1
        sale.payment_status = "due"
        line = sale.lines[0]
        line.quantity = 1
        line.product_id = line.variation_id = 554
        line.unit_price_inc_tax = 25000
        self.frappe.session.user = "Administrator"
        self.frappe.get_doc.side_effect = lambda doctype, name, **kwargs: sale if doctype == "Kit POS Sale" else invoice
        self.db.get_value.side_effect = lambda doctype, *args, **kwargs: (2, 25000, 50000) if doctype == "Bin" else 0
        for name, value in (("_require_schema", None), ("_trusted_source", (sale, {})), ("_lock_inventory", None),
                            ("_pilot_item_and_bom", {**recipe(), "bom": "BOM-VXN-GOAT-PORTION-001", "quantity": "1", "uom": "Nos"}),
                            ("_existing", None), ("_available", Decimal(2))):
            self.enterContext(patch.object(module, name, return_value=value))
        new = self.enterContext(patch.object(module, "_new_work_order", return_value=Doc(name="MFG-WO-PILOT")))
        with patch.object(ids, "lookup", return_value=module.PILOT["item_code"]) as lookup:
            self.assertEqual(module.plan_legacy_pilot(), "MFG-WO-PILOT")
            self.assertEqual(lookup.call_count, 2)
        self.assertTrue(new.call_args.args[1]["planning_only"])
        self.assertEqual(new.call_args.args[1]["production_item"], module.PORTION)
        self.db.commit.assert_not_called()
        line.unit_price_inc_tax = 20000
        with patch.object(ids, "lookup", return_value=module.PILOT["item_code"]), self.assertRaises(module.ProductionError):
            module.plan_legacy_pilot()

    def _engine(self, *, existing=False, available=Decimal("2")):
        invoice = self.invoice()
        expected = plan()
        wo = work_order(expected)
        for name, value in (("_require_schema", None), ("_trusted_source", (self.source(), {})),
                            ("_lock_inventory", None), ("_matched_lines", [(invoice.items[0], self.source().lines[0])]),
                            ("_plan", expected), ("_existing", wo if existing else None),
                            ("_validate_work_order", None), ("_manufacture_for", None), ("_available", available),
                            ("_actual", Decimal(1) if existing else Decimal(0))):
            self.enterContext(patch.object(module, name, return_value=value))
        new = self.enterContext(patch.object(module, "_new_work_order", return_value=wo))
        complete = self.enterContext(patch.object(module, "_complete_work_order", return_value=wo))
        if existing:
            invoice.name = "SI-ORIGINAL"
        return invoice, new, complete

    def test_new_order_calls_one_native_work_order_and_one_manufacture(self):
        invoice, new, complete = self._engine()
        self.assertEqual(module.ensure_invoice_production(invoice), ["MFG-WO-TEST"])
        new.assert_called_once()
        complete.assert_called_once()
        self.db.commit.assert_not_called()
        self.assertEqual(invoice.update_stock, 1)

    def test_payment_only_retry_reuses_wo_without_recooking(self):
        invoice, new, complete = self._engine(existing=True)
        original = deepcopy(invoice)
        original.docstatus = 2
        setattr(original.items[0], module.KEY_FIELD, module.production_key(plan()["sale_uuid"], plan()["line_uuid"]))
        setattr(original.items[0], module.WORK_ORDER_FIELD, "MFG-WO-TEST")
        self.frappe.get_doc.return_value = original
        invoice.name = "SI-AMENDED-PAYMENT-ONLY"
        invoice.amended_from = "SI-ORIGINAL"
        self.assertEqual(module.ensure_invoice_production(invoice), ["MFG-WO-TEST"])
        new.assert_not_called()
        complete.assert_not_called()
        self.db.commit.assert_not_called()

    def test_global_negative_stock_does_not_override_scoped_shortage(self):
        invoice, new, complete = self._engine(available=Decimal("0.2"))
        with self.assertRaisesRegex(module.ProductionError, "Kitchen stock is insufficient"):
            module.ensure_invoice_production(invoice)
        new.assert_not_called()
        complete.assert_not_called()

    def test_reused_work_order_still_requires_available_finished_stock(self):
        invoice, new, complete = self._engine(existing=True, available=Decimal("0"))
        with self.assertRaisesRegex(module.ProductionError, "Kitchen stock is insufficient"):
            module.ensure_invoice_production(invoice)
        new.assert_not_called()
        complete.assert_not_called()

    def test_return_cannot_manufacture(self):
        invoice, new, complete = self._engine()
        invoice.is_return = 1
        with self.assertRaises(module.ProductionError):
            module.ensure_invoice_production(invoice)
        new.assert_not_called()
        complete.assert_not_called()

    def test_new_origin_cannot_spend_another_cancelled_finished_dish(self):
        invoice, new, complete = self._engine()
        with patch.object(module, "_actual", return_value=Decimal(1)):
            with self.assertRaises(module.ProductionError):
                module.ensure_invoice_production(invoice)
        new.assert_not_called()
        complete.assert_not_called()

    def test_reuse_cannot_borrow_pooled_excess_finished_dishes(self):
        invoice, new, complete = self._engine(existing=True)
        with patch.object(module, "_actual", return_value=Decimal(2)):
            with self.assertRaises(module.ProductionError):
                module.ensure_invoice_production(invoice)
        new.assert_not_called()
        complete.assert_not_called()

    def test_two_new_same_style_lines_check_initial_stock_only_once(self):
        invoice, new, complete = self._engine()
        second_row = deepcopy(invoice.items[0])
        second_row.name = "SII-NEW-2"
        invoice.items.append(second_row)
        second_plan = {**plan(line="18d310b5-8c03-4ce7-9503-8e54e975d81f"), "line_id": "15807"}
        second_line = Doc(line_id=15807, desktop_line_uuid=second_plan["line_uuid"])
        second_wo = work_order(second_plan)
        second_wo.name = "MFG-WO-SECOND"
        new.side_effect = [work_order(), second_wo]
        complete.side_effect = lambda wo, _plan, _context: wo
        with patch.object(module, "_plan", side_effect=[plan(), second_plan]), patch.object(module, "_matched_lines", return_value=[(invoice.items[0], self.source().lines[0]), (second_row, second_line)]), patch.object(module, "_actual", return_value=Decimal(0)) as actual:
            self.assertEqual(module.ensure_invoice_production(invoice), ["MFG-WO-TEST", "MFG-WO-SECOND"])
            actual.assert_called_once_with("VXN-DRY-GOAT")
        self.assertEqual(new.call_count, 2)
        self.assertEqual(complete.call_count, 2)
