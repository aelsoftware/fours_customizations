"""Write-free checks of the exact, never-produced Vox pilot retirement.

The root-owned native rollback probe is still required before any live use.
These fixtures exercise permissions, source/snapshot fences and postconditions;
they do not substitute a mock cancellation for ERPNext integration testing.
"""

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


def original_snapshot():
    return {"company": module.COMPANY, "instructions": [], "line_id": "15806",
            "line_uuid": "28d310b5-8c03-4ce7-9503-8e54e975d81f", "modifier_reference": "",
            "planning_only": True, "posting_datetime": "2026-09-28 23:09:40",
            "production_item": module.PORTION, "quantity": "1", "raw_quantity": "0.4",
            "recipe": {"bom": "BOM-VXN-GOAT-PORTION-001", "modified": "2026-09-29 00:21:57.584214",
                       "quantity": "1", "raw_item": module.RAW, "raw_per_portion": "0.4",
                       "raw_qty": "0.4", "raw_uom": "Kg", "uom": "Nos", "warehouse": module.WAREHOUSE},
            "sale": "4929", "sale_label": "KOD-2-101",
            "sale_uuid": "a0812a30-f0c7-443d-803b-3115249811c2", "source_item": "VX-0128",
            "uom": "Nos", "version": 1}


def original_work_order():
    return Doc(doctype="Work Order", name="MFG-WO-2026-00001", company=module.COMPANY,
               docstatus=1, status="Not Started", production_item=module.PORTION,
               bom_no="BOM-VXN-GOAT-PORTION-001", qty=1, stock_uom="Nos",
               source_warehouse=module.WAREHOUSE, fg_warehouse=module.WAREHOUSE,
               skip_transfer=1, transfer_material_against="Work Order", planned_start_date="2026-09-28 23:09:40",
               produced_qty=0, material_transferred_for_manufacturing=0, additional_transferred_qty=0,
               process_loss_qty=0, disassembled_qty=0, operations=[], from_wip_warehouse=0,
               reserve_stock=0, track_semi_finished_goods=0, use_multi_level_bom=0,
               flags=Doc(ignore_links=False), check_no_back_links_exist=Mock(),
               required_items=[Doc(item_code=module.RAW, stock_uom="Kg", source_warehouse=module.WAREHOUSE,
                                   required_qty=0.4, include_item_in_manufacturing=1, consumed_qty=0,
                                   transferred_qty=0, returned_qty=0, stock_reserved_qty=0)],
               **{module.KEY_FIELD: module.production_key(module.PILOT["sale_uuid"], module.PILOT["line_uuid"]),
                  module.SNAPSHOT_FIELD: module._canonical(original_snapshot()), module.INVOICE_FIELD: "SI-2640-198"})


class TestVoxPilotRetirement(TestCase):
    def setUp(self):
        from kit_pos.connector import ids, materialize

        self.db = SimpleNamespace(sql=Mock(side_effect=self._sql), get_value=Mock(return_value=0),
                                  commit=Mock(), auto_commit_on_many_writes=False,
                                  _disable_transaction_control=0, set_value=Mock())
        self.frappe = SimpleNamespace(db=self.db, session=SimpleNamespace(user="Administrator"),
                                     local=SimpleNamespace(site=module.SITE), get_doc=Mock(side_effect=self._get_doc))
        self.enterContext(patch.object(module, "frappe", self.frappe))
        self.enterContext(patch.object(module, "_require_schema"))
        self.inventory_lock = self.enterContext(patch.object(module, "_lock_inventory"))
        self.source_lock = self.enterContext(patch.object(materialize, "_locked_sale", side_effect=lambda _: self.sale))
        self.trusted = self.enterContext(patch.object(module, "_trusted_source", side_effect=lambda *a, **k: (self.sale, {})))
        self.projection = self.enterContext(patch.object(module, "_matched_lines", return_value=[]))
        self.lookup = self.enterContext(patch.object(ids, "lookup", return_value=module.PILOT["item_code"]))
        self.enterContext(patch.object(module, "now_datetime", return_value=datetime(2026, 9, 29, 12)))
        self.new_work_order = self.enterContext(patch.object(module, "_new_work_order"))
        self.manufacture = self.enterContext(patch.object(module, "_complete_work_order"))
        self._reset_documents()

    def _reset_documents(self):
        self.wo = original_work_order()
        self.wo.cancel = Mock(side_effect=self._native_cancel)
        self.sale = Doc(name=4929, sale_uuid=module.PILOT["sale_uuid"], invoice_no="KOD-2-101",
                        sales_invoice="SI-2640-198", final_total=49000, amount_paid=0, payments=[],
                        status="final", is_suspend=1, payment_status="due",
                        lines=[Doc(line_id=15806, desktop_line_uuid=module.PILOT["line_uuid"], item_code="VX-0128",
                                   quantity=1, unit_price=25000, unit_price_inc_tax=25000, product_id=554, variation_id=554)])
        self.invoice = Doc(doctype="Sales Invoice", name="SI-2640-198", company=module.COMPANY,
                           docstatus=0, status="Draft", grand_total=49000, paid_amount=0, payments=[],
                           posting_date="2026-09-28", posting_time="23:09:40",
                           items=[Doc(item_code="VX-0128", qty=1, rate=25000, uom="Pc(s)",
                                      stock_uom="Pc(s)", conversion_factor=1)])
        self.bins = {module.RAW: tuple(map(Decimal, ("2", "50000", "25000", "0.8", "0"))),
                     module.PORTION: tuple(map(Decimal, ("0", "0", "0", "0", "1")))}
        self.activity = {}

    def _get_doc(self, doctype, name, **kwargs):
        self.assertTrue(kwargs.get("for_update"), "All retirement document reads must be current locking reads.")
        return {"Work Order": self.wo, "Kit POS Sale": self.sale, "Sales Invoice": self.invoice}[doctype]

    def _sql(self, query, values):
        self.assertIn("FOR UPDATE", query)
        if "FROM tabBin " in query:
            return [self.bins[values[0]]] if values[0] in self.bins else []
        for marker, result in self.activity.items():
            if marker in query:
                return result
        return []

    def _native_cancel(self):
        # Document._cancel -> before_cancel -> WorkOrder.on_cancel. Only the
        # root's rollback probe executes the actual ERPNext lifecycle.
        self.wo.docstatus = 2
        context = module._WRITING.get()
        self.assertIs(context.retiring_pilot, self.wo)
        self.assertIs(context.objects[id(self.wo)], self.wo)
        module.guard_generated_document(self.wo, "before_cancel")
        self.wo.status = "Cancelled"
        raw, portion = self.bins[module.RAW], self.bins[module.PORTION]
        self.bins[module.RAW] = (*raw[:3], raw[3] - Decimal("0.4"), raw[4])
        self.bins[module.PORTION] = (*portion[:4], portion[4] - Decimal(1))

    def _assert_no_cancellation(self):
        with self.assertRaises(module.ProductionError):
            module._retire_legacy_pilot_work_order()
        self.wo.cancel.assert_not_called()
        self.db.commit.assert_not_called()
        self.db.set_value.assert_not_called()

    def test_original_snapshot_is_exactly_pinned_and_all_states_are_distinct(self):
        self.assertEqual(module._digest(original_snapshot()), module._PILOT_PLANNING_SNAPSHOT_SHA256)
        module._validate_pilot_retirement_work_order(self.wo, state="submitted")
        self.wo.docstatus = 2
        module._validate_pilot_retirement_work_order(self.wo, state="cancelling")
        with self.assertRaises(module.ProductionError):
            module._validate_pilot_retirement_work_order(self.wo, state="cancelled")
        self.wo.status = "Cancelled"
        module._validate_pilot_retirement_work_order(self.wo, state="cancelled")

    def test_native_cancel_releases_only_own_hold_without_stock_source_or_key_changes(self):
        source_before = module._digest([self.sale.as_dict(), self.invoice.as_dict()])
        key, frozen = self.wo.get(module.KEY_FIELD), self.wo.get(module.SNAPSHOT_FIELD)
        result = module._retire_legacy_pilot_work_order()
        self.assertEqual(result, {"work_order": "MFG-WO-2026-00001", "already_cancelled": False,
                                  "production_key": key, "raw_reservation_released": "0.4",
                                  "finished_planned_quantity_released": "1"})
        self.assertEqual(self.wo.get(module.KEY_FIELD), key)
        self.assertEqual(self.wo.get(module.SNAPSHOT_FIELD), frozen)
        self.assertEqual(self.bins[module.RAW], tuple(map(Decimal, ("2", "50000", "25000", "0.4", "0"))))
        self.assertEqual(self.bins[module.PORTION][-1], 0)
        self.assertEqual(module._digest([self.sale.as_dict(), self.invoice.as_dict()]), source_before)
        self.wo.cancel.assert_called_once_with()
        self.db.commit.assert_not_called()
        self.db.set_value.assert_not_called()
        self.new_work_order.assert_not_called()
        self.manufacture.assert_not_called()
        self.assertIsNone(module._WRITING.get())

    def test_repeat_after_bill_correction_is_verified_noop_not_second_release(self):
        module._retire_legacy_pilot_work_order()
        self.sale.final_total = self.invoice.grand_total = 54000
        self.sale.lines = []  # old line was persistently tombstoned by caller
        self.invoice.items = [Doc(item_code="VXN-PAN-FRIED-GOAT")]
        before = deepcopy(self.bins)
        result = module._retire_legacy_pilot_work_order()
        self.assertTrue(result["already_cancelled"])
        self.assertEqual(result["raw_reservation_released"], "0")
        self.assertEqual(result["finished_planned_quantity_released"], "0")
        self.assertEqual(self.bins, before)
        self.wo.cancel.assert_called_once_with()
        self.assertEqual(self.trusted.call_count, 1)
        self.db.commit.assert_not_called()

    def test_unauthorized_actor_or_site_is_rejected_before_any_locks(self):
        for user, site in (("cashier@example.com", module.SITE), ("Guest", module.SITE),
                           ("Administrator", "ddamu.kit.africa")):
            self.frappe.session.user, self.frappe.local.site = user, site
            with self.subTest(user=user, site=site):
                self._assert_no_cancellation()
        self.source_lock.assert_not_called()
        self.frappe.get_doc.assert_not_called()

    def test_exact_source_identity_and_company_are_required_even_for_noop(self):
        for docstatus in (1, 2):
            for target, field, invalid in (("sale", "name", 2128), ("sale", "sale_uuid", "different"),
                                           ("sale", "sales_invoice", "SI-2637-721"),
                                           ("sale", "invoice_no", "KOD-2-102"),
                                           ("invoice", "name", "SI-OTHER"),
                                           ("invoice", "company", "4S Industries Limited")):
                self._reset_documents()
                self.wo.docstatus = docstatus
                self.wo.status = "Cancelled" if docstatus == 2 else "Not Started"
                setattr(getattr(self, target), field, invalid)
                with self.subTest(docstatus=docstatus, target=target, field=field):
                    self._assert_no_cancellation()

    def test_actual_cancel_requires_original_49k_held_unpaid_draft(self):
        changes = (("sale", "is_suspend", 0), ("sale", "payment_status", "paid"),
                   ("sale", "amount_paid", 1), ("sale", "payments", [Doc(amount=0)]),
                   ("sale", "final_total", 54000), ("invoice", "docstatus", 1),
                   ("invoice", "status", "Paid"), ("invoice", "grand_total", 54000),
                   ("invoice", "paid_amount", 1), ("invoice", "payments", [Doc(amount=0)]),
                   ("invoice", "amended_from", "SI-OTHER"), ("invoice", "posting_time", "23:09:41"))
        for target, field, value in changes:
            self._reset_documents()
            setattr(getattr(self, target), field, value)
            with self.subTest(target=target, field=field):
                self._assert_no_cancellation()

    def test_source_line_cannot_be_reidentified_repriced_or_changed_to_new_dish(self):
        for field, value in (("line_id", 15807), ("desktop_line_uuid", "other"), ("item_code", "VXN-PAN-FRIED-GOAT"),
                             ("quantity", 2), ("unit_price", 20000), ("unit_price_inc_tax", 30000),
                             ("product_id", 555), ("variation_id", 555)):
            self._reset_documents()
            setattr(self.sale.lines[0], field, value)
            with self.subTest(field=field):
                self._assert_no_cancellation()
        self._reset_documents()
        self.sale.lines.append(deepcopy(self.sale.lines[0]))
        self._assert_no_cancellation()

    def test_registry_projection_and_source_trust_fail_closed(self):
        self.lookup.return_value = "VXN-PAN-FRIED-GOAT"
        self._assert_no_cancellation()
        self.lookup.return_value = "VX-0128"
        self.projection.side_effect = module.ProductionError(module.CHANGED)
        self._assert_no_cancellation()
        self.projection.side_effect = None
        self.trusted.side_effect = module.ProductionError(module.UNVERIFIED)
        self._assert_no_cancellation()

    def test_draft_legacy_row_must_still_be_original_nonstock_unmodified_portion(self):
        for field, value in (("item_code", "VXN-PAN-FRIED-GOAT"), ("qty", 2), ("rate", 30000),
                             ("uom", "Kg"), ("stock_uom", "Kg"), ("conversion_factor", 10),
                             (module.MODIFIER_FIELD, "unexpected")):
            self._reset_documents()
            setattr(self.invoice.items[0], field, value)
            with self.subTest(field=field):
                self._assert_no_cancellation()
        self._reset_documents()
        self.db.get_value.return_value = 1
        self._assert_no_cancellation()

    def test_wrong_wo_completed_production_and_foreign_links_are_never_cancellable(self):
        changes = (("name", "MFG-WO-2026-00002"), ("company", "Vox Lounge"),
                   ("production_item", "VXN-PAN-FRIED-GOAT"), ("bom_no", "BOM-VXN-PAN-FRIED-GOAT-001"),
                   ("qty", 2), ("stock_uom", "Pc(s)"), ("source_warehouse", "Other"), ("fg_warehouse", "Other"),
                   ("docstatus", 0), ("status", "Completed"), ("produced_qty", 1),
                   ("material_transferred_for_manufacturing", 1), ("additional_transferred_qty", 1),
                   ("process_loss_qty", 0.1), ("disassembled_qty", 1), ("reserve_stock", 1),
                   ("track_semi_finished_goods", 1), ("operations", [Doc(name="op")]),
                   ("from_wip_warehouse", 1), ("wip_warehouse", "WIP"), ("skip_transfer", 0),
                   ("transfer_material_against", "Job Card"), ("planned_start_date", "2026-09-29 00:00:00"),
                   ("sales_order", "SO-1"), ("sales_invoice", "SI-OTHER"), ("material_request", "MR-1"),
                   ("production_plan", "PP-1"), ("subcontracting_inward_order", "SC-1"), ("amended_from", "WO-OTHER"),
                   (module.KEY_FIELD, "VXWO-OTHER"), (module.INVOICE_FIELD, "SI-OTHER"))
        for field, value in changes:
            self._reset_documents()
            setattr(self.wo, field, value)
            with self.subTest(field=field):
                self._assert_no_cancellation()

    def test_changed_snapshot_or_consumption_is_rejected_on_submitted_and_cancelled_records(self):
        for docstatus in (1, 2):
            for field, value in (("planning_only", False), ("quantity", "2"), ("source_item", "VXN-PAN-FRIED-GOAT"),
                                 ("instructions", [{"label": "Chips"}]), ("sale", "2128"),
                                 ("recipe", {**original_snapshot()["recipe"], "modified": "later"})):
                self._reset_documents()
                self.wo.docstatus = docstatus
                self.wo.status = "Cancelled" if docstatus == 2 else "Not Started"
                setattr(self.wo, module.SNAPSHOT_FIELD, module._canonical({**original_snapshot(), field: value}))
                with self.subTest(docstatus=docstatus, field=field):
                    self._assert_no_cancellation()
            self._reset_documents()
            self.wo.docstatus = docstatus
            self.wo.status = "Cancelled" if docstatus == 2 else "Not Started"
            self.wo.required_items[0].consumed_qty = 0.4
            self._assert_no_cancellation()

    def test_exact_raw_row_with_zero_consumption_transfer_and_reservations_is_required(self):
        for field, value in (("item_code", "OTHER"), ("stock_uom", "Nos"), ("source_warehouse", "Other"),
                             ("required_qty", 0.8), ("include_item_in_manufacturing", 0),
                             ("consumed_qty", 0.4), ("transferred_qty", 0.4), ("returned_qty", 0.4),
                             ("stock_reserved_qty", 0.4), ("allow_alternative_item", 1),
                             ("operation", "cook"), ("is_additional_item", 1), ("is_customer_provided_item", 1),
                             ("voucher_detail_reference", "FOREIGN")):
            self._reset_documents()
            setattr(self.wo.required_items[0], field, value)
            with self.subTest(field=field):
                self._assert_no_cancellation()

    def test_any_production_history_even_draft_or_cancelled_blocks_retirement(self):
        markers = ("`tabStock Entry`", "`tabJob Card`", "`tabStock Reservation Entry`", "`tabStock Ledger Entry`",
                   "`tabGL Entry`", "`tabSales Invoice Item`")
        for marker in markers:
            for docstatus in (0, 1, 2):
                self._reset_documents()
                self.activity[marker] = [("EXISTING", docstatus)]
                with self.subTest(marker=marker, docstatus=docstatus):
                    self._assert_no_cancellation()

    def test_active_material_request_blocks_before_cancel_but_cancelled_history_is_kept(self):
        for docstatus in (0, 1):
            self._reset_documents()
            self.activity["`tabMaterial Request`"] = [("MAT-MR-2026-00001", docstatus)]
            with self.subTest(docstatus=docstatus):
                self._assert_no_cancellation()
            self.wo.check_no_back_links_exist.assert_not_called()
        self._reset_documents()
        self.activity["`tabMaterial Request`"] = [("MAT-MR-2026-00001", 2)]
        module._retire_legacy_pilot_work_order()
        self.assertEqual(self.activity["`tabMaterial Request`"], [("MAT-MR-2026-00001", 2)])
        self.wo.cancel.assert_called_once_with()

    def test_native_backlinks_are_preflighted_without_ignore_flags(self):
        self.wo.check_no_back_links_exist.side_effect = module.ProductionError("linked document")
        self._assert_no_cancellation()
        for field, value in (("ignore_links", True), ("ignore_linked_doctypes", ["Material Request"])):
            self._reset_documents()
            if field == "ignore_links":
                setattr(self.wo.flags, field, value)
            else:
                setattr(self.wo, field, value)
            with self.subTest(field=field):
                self._assert_no_cancellation()

    def test_insufficient_or_missing_native_bin_holds_block_before_cancel(self):
        for item, index in ((module.RAW, 3), (module.PORTION, 4)):
            self._reset_documents()
            values = list(self.bins[item])
            values[index] = Decimal(0)
            self.bins[item] = tuple(values)
            self._assert_no_cancellation()
        self._reset_documents()
        del self.bins[module.RAW]
        self._assert_no_cancellation()

    def test_postcondition_rejects_every_wrong_bin_delta_and_requires_caller_rollback(self):
        for item in (module.RAW, module.PORTION):
            for index in range(5):
                self._reset_documents()
                def wrong_delta(item=item, index=index):
                    self._native_cancel()
                    values = list(self.bins[item])
                    values[index] += Decimal(1)
                    self.bins[item] = tuple(values)
                self.wo.cancel.side_effect = wrong_delta
                with self.subTest(item=item, index=index), self.assertRaises(module.ProductionError):
                    module._retire_legacy_pilot_work_order()
                self.db.commit.assert_not_called()
                self.assertIsNone(module._WRITING.get())

    def test_postcondition_rejects_native_changes_to_source_or_new_stock_entry(self):
        for kind in ("invoice", "stock_entry", "produced", "snapshot_formatting"):
            self._reset_documents()
            def unexpected_activity(kind=kind):
                self._native_cancel()
                if kind == "invoice":
                    self.invoice.grand_total = 54000
                elif kind == "stock_entry":
                    self.activity["`tabStock Entry`"] = [("STE-UNEXPECTED",)]
                elif kind == "produced":
                    self.wo.produced_qty = 1
                else:
                    setattr(self.wo, module.SNAPSHOT_FIELD, " " + self.wo.get(module.SNAPSHOT_FIELD))
            self.wo.cancel.side_effect = unexpected_activity
            with self.subTest(kind=kind), self.assertRaises(module.ProductionError):
                module._retire_legacy_pilot_work_order()
            self.db.commit.assert_not_called()
            self.assertIsNone(module._WRITING.get())

    def test_commit_is_prohibited_during_native_cancel_and_context_always_restored(self):
        commit = self.db.commit
        self.wo.cancel.side_effect = lambda: self.db.commit()
        with self.assertRaises(module.ProductionError):
            module._retire_legacy_pilot_work_order()
        self.assertIs(self.db.commit, commit)
        self.assertIsNone(module._WRITING.get())
        commit.assert_not_called()

    def test_retirement_capability_is_extra_object_bound_and_never_allows_trash(self):
        self.wo.docstatus = 2  # native pre-cancel state
        with self.assertRaises(module.ProductionError):
            module.guard_generated_document(self.wo, "before_cancel")
        with module._native_writes() as context:
            context.objects[id(self.wo)] = self.wo
            with self.assertRaises(module.ProductionError):
                module.guard_generated_document(self.wo, "before_cancel")
            context.retiring_pilot = self.wo
            module.guard_generated_document(self.wo, "before_cancel")
            cloned = original_work_order()
            cloned.docstatus = 2
            context.objects[id(cloned)] = cloned
            for doc, method in ((cloned, "before_cancel"), (self.wo, "on_trash")):
                with self.subTest(method=method), self.assertRaises(module.ProductionError):
                    module.guard_generated_document(doc, method)

    def test_retirement_capability_cannot_cancel_new_dish_other_wo_or_stock_entry(self):
        for field, value in (("production_item", "VXN-PAN-FRIED-GOAT"), ("name", "MFG-WO-2026-00002")):
            self._reset_documents()
            self.wo.docstatus = 2
            setattr(self.wo, field, value)
            with module._native_writes() as context:
                context.objects[id(self.wo)] = context.retiring_pilot = self.wo
                with self.subTest(field=field), self.assertRaises(module.ProductionError):
                    module.guard_generated_document(self.wo, "before_cancel")
        entry = Doc(doctype="Stock Entry", name="STE-1", work_order="MFG-WO-2026-00001")
        with patch.object(module, "_marked_work_order", return_value=True), module._native_writes() as context:
            context.objects[id(entry)] = context.retiring_pilot = entry
            with self.assertRaises(module.ProductionError):
                module.guard_generated_document(entry, "before_cancel")

    def test_retirement_capability_remains_actor_site_and_database_bound(self):
        for changed in ("actor", "site", "database"):
            self._reset_documents()
            self.wo.docstatus = 2
            with module._native_writes() as context:
                context.objects[id(self.wo)] = context.retiring_pilot = self.wo
                if changed == "actor":
                    self.frappe.session.user = "cashier@example.com"
                elif changed == "site":
                    self.frappe.local.site = "ddamu.kit.africa"
                else:
                    context.database = object()
                with self.subTest(changed=changed), self.assertRaises(module.ProductionError):
                    module.guard_generated_document(self.wo, "before_cancel")
            self.frappe.session.user = "Administrator"
            self.frappe.local.site = module.SITE

    def test_no_activity_checks_use_current_locks_and_no_docstatus_filter(self):
        module._retire_legacy_pilot_work_order()
        queries = [call.args[0] for call in self.db.sql.call_args_list]
        self.assertTrue(queries and all("FOR UPDATE" in query for query in queries))
        for query in queries:
            if "`tabStock Entry`" in query or "`tabJob Card`" in query:
                self.assertNotIn("docstatus", query)
        self.source_lock.assert_called_once_with("4929")
        self.inventory_lock.assert_called_once_with()
