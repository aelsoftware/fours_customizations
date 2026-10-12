"""Standalone write-free tests for the one approved native MR cancellation."""

from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from fours_customizations import vox_pilot_material_request as module
from fours_customizations import vox_work_orders as production


class Doc(SimpleNamespace):
    def get(self, name, default=None):
        return self.__dict__.get(name, default)

    def as_dict(self):
        def plain(value):
            if isinstance(value, Doc):
                return {key: plain(part) for key, part in value.__dict__.items()
                        if key != "flags" and not callable(part)}
            if isinstance(value, list):
                return [plain(part) for part in value]
            return deepcopy(value)
        return plain(self)


class TestVoxPilotMaterialRequest(TestCase):
    def setUp(self):
        from frappe.model import delete_doc, dynamic_links, rename_doc
        from kit_pos.connector import materialize

        self.db = SimpleNamespace(sql=Mock(side_effect=self._sql), commit=Mock(), set_value=Mock(),
                                  auto_commit_on_many_writes=False, _disable_transaction_control=0)
        self.frappe = SimpleNamespace(db=self.db, session=SimpleNamespace(user="Administrator"),
                                     local=SimpleNamespace(site=module.SITE),
                                     get_doc=Mock(side_effect=self._get_doc),
                                     get_hooks=Mock(return_value=[]), get_meta=Mock(side_effect=self._get_meta))
        self.enterContext(patch.object(module, "frappe", self.frappe))
        self.enterContext(patch.object(production, "frappe", self.frappe))
        self.source_lock = self.enterContext(patch.object(materialize, "_locked_sale", side_effect=lambda _: self.sale))
        self.inventory_lock = self.enterContext(patch.object(production, "_lock_inventory"))
        self.wo_validation = self.enterContext(patch.object(production, "_validate_pilot_retirement_work_order"))
        self.native_static = self.enterContext(patch.object(delete_doc, "get_linked_docs", return_value=[]))
        self.native_dynamic = self.enterContext(patch.object(delete_doc, "get_dynamic_linked_docs", return_value=[]))
        self.static_metadata = self.enterContext(patch.object(rename_doc, "get_link_fields", return_value=[
            {"parent": name, "fieldname": "material_request", "issingle": 0}
            for name in ("Purchase Order Item", "Purchase Receipt Item", "Purchase Invoice Item", "Stock Entry Detail")]))
        self.dynamic_metadata = self.enterContext(patch.object(dynamic_links, "get_dynamic_link_map", return_value={}))
        self._reset_documents()

    def _reset_documents(self):
        self.mr = Doc(doctype="Material Request", name=module.REQUEST, company=module.COMPANY,
                      material_request_type="Purchase", work_order=module.WORK_ORDER,
                      docstatus=1, status="Pending", owner="Administrator",
                      creation="2026-09-29 10:34:52.918339", modified="2026-09-29 10:34:56.729587",
                      modified_by="Administrator", transaction_date="2026-09-29", schedule_date="2026-09-29",
                      per_ordered=0, per_received=0, flags=Doc(), items=[])
        self.mr.check_no_back_links_exist = Mock()
        self.mr.cancel = Mock(side_effect=self._native_cancel)
        for idx, (name, code, uom, quantity, bom) in enumerate(module.ROWS, 1):
            self.mr.items.append(Doc(name=name, idx=idx, parent=module.REQUEST, parenttype="Material Request",
                parentfield="items", docstatus=1, item_code=code, warehouse=module.WAREHOUSE, uom=uom, stock_uom=uom,
                qty=quantity, stock_qty=quantity, conversion_factor=1, bom_no=bom, schedule_date="2026-09-29",
                ordered_qty=0, received_qty=0, picked_qty=0, rate=0, price_list_rate=0, amount=0,
                modified="2026-09-29 10:34:56.729587", modified_by="Administrator"))
        self.sale = Doc(doctype="Kit POS Sale", name=4929, sale_uuid=module.SALE_UUID, sales_invoice=module.INVOICE,
                        final_total=49000, amount_paid=0)
        self.invoice = Doc(doctype="Sales Invoice", name=module.INVOICE, company=module.COMPANY, docstatus=0,
                           grand_total=49000, paid_amount=0)
        self.wo = Doc(doctype="Work Order", name=module.WORK_ORDER, docstatus=1, status="Not Started", produced_qty=0)
        self.items = {code: Doc(doctype="Item", name=code, is_stock_item=1, stock_uom=uom)
                      for _name, code, uom, _quantity, _bom in module.ROWS}
        self.warehouse = Doc(doctype="Warehouse", name=module.WAREHOUSE, company=module.COMPANY, disabled=0, is_group=0)
        self.bins = {module.RAW: dict(zip(module.BIN_FIELDS, map(Decimal, ("2", "50000", "25000", "0.4", "0", "0", "0.4", "2")))),
                     module.PORTION: dict(zip(module.BIN_FIELDS, map(Decimal, ("0", "0", "0", "1", "0", "1", "0", "2"))))}
        self.activity = {}

    def _get_doc(self, doctype, name, **kwargs):
        if doctype == "Item":
            return self.items[name]
        return {"Material Request": self.mr, "Work Order": self.wo, "Kit POS Sale": self.sale,
                "Sales Invoice": self.invoice, "Warehouse": self.warehouse}[doctype]

    def _get_meta(self, doctype):
        return Doc(issingle=doctype == "Test Singleton", has_field=lambda field: field == "material_request_item")

    def _sql(self, query, values):
        if " FROM tabBin " in query:
            return [tuple(self.bins[values[0]][field] for field in module.BIN_FIELDS)] if values[0] in self.bins else []
        for marker, rows in self.activity.items():
            if marker in query:
                return rows
        return []

    def _native_cancel(self):
        self.assertIsNotNone(production._WRITING.get())
        self.mr.docstatus, self.mr.status = 2, "Cancelled"
        self.mr.modified = "2026-09-29 15:00:00"
        for row in self.mr.items:
            row.docstatus = 2
            row.modified = "2026-09-29 15:00:00"
        for _name, code, _uom, quantity, _bom in module.ROWS:
            self.bins[code]["indented_qty"] -= quantity
            self.bins[code]["projected_qty"] -= quantity

    def _token(self):
        return module.preview()["digest"]

    def _assert_refused(self, token="a" * 64):
        with self.assertRaises((module.RequestCancellationError, production.ProductionError)):
            module.cancel_request(token)
        self.mr.cancel.assert_not_called()
        self.db.commit.assert_not_called()
        self.db.set_value.assert_not_called()

    def test_preview_is_readonly_and_nonlocking(self):
        before = self.mr.as_dict(), deepcopy(self.bins)
        result = module.preview()
        self.assertEqual(result["material_request"], module.REQUEST)
        self.assertFalse(result["already_cancelled"])
        self.assertEqual(result["requested_quantities"], {module.RAW: "0.4", module.PORTION: "1"})
        self.assertEqual((self.mr.as_dict(), self.bins), before)
        self.assertRegex(result["digest"], r"^[0-9a-f]{64}$")
        self.assertFalse(any("FOR UPDATE" in call.args[0] for call in self.db.sql.call_args_list))
        self.source_lock.assert_not_called()
        self.inventory_lock.assert_not_called()
        self.mr.cancel.assert_not_called()
        self.db.commit.assert_not_called()

    def test_only_native_cancel_releases_requested_qty_not_wo_holds_stock_or_history(self):
        before_business = module._request_business(self.mr)
        token = self._token()
        result = module.cancel_request(token)
        self.assertFalse(result["already_cancelled"])
        self.assertEqual(result["released_indented_qty"], {module.RAW: "0.4", module.PORTION: "1"})
        self.assertFalse(result["stock_movement_created"])
        self.assertEqual(result["before_digest"], token)
        self.assertNotEqual(result["after_digest"], token)
        self.assertEqual(module._request_business(self.mr), before_business)
        self.assertEqual(self.bins[module.RAW]["actual_qty"], 2)
        self.assertEqual(self.bins[module.RAW]["stock_value"], 50000)
        self.assertEqual(self.bins[module.RAW]["indented_qty"], 0)
        self.assertEqual(self.bins[module.RAW]["reserved_qty_for_production"], Decimal("0.4"))
        self.assertEqual(self.bins[module.PORTION]["actual_qty"], 0)
        self.assertEqual(self.bins[module.PORTION]["indented_qty"], 0)
        self.assertEqual(self.bins[module.PORTION]["planned_qty"], 1)
        self.assertEqual(self.mr.work_order, module.WORK_ORDER)
        self.mr.cancel.assert_called_once_with()
        self.db.commit.assert_not_called()
        self.db.set_value.assert_not_called()
        self.assertIsNone(production._WRITING.get())

    def test_verified_cancelled_retry_with_original_token_never_releases_twice(self):
        token = self._token()
        module.cancel_request(token)
        # Later exact-order correction/WO retirement do not authorize another
        # request cancellation and are not artificially reversed for a retry.
        self.wo.docstatus, self.wo.status = 2, "Cancelled"
        self.invoice.grand_total = self.sale.final_total = 54000
        self.bins[module.RAW]["reserved_qty_for_production"] = Decimal(0)
        self.bins[module.RAW]["projected_qty"] += Decimal("0.4")
        self.bins[module.PORTION]["planned_qty"] = Decimal(0)
        self.bins[module.PORTION]["projected_qty"] -= Decimal(1)
        before = deepcopy(self.bins)
        result = module.cancel_request(token)
        self.assertTrue(result["already_cancelled"])
        self.assertEqual(result["released_indented_qty"], {module.RAW: "0", module.PORTION: "0"})
        self.assertEqual(self.bins, before)
        self.mr.cancel.assert_called_once_with()

    def test_permission_site_and_auto_commit_guard_runs_before_reads(self):
        for user, site, auto in (("cashier", module.SITE, False), ("Administrator", "ddamu.kit.africa", False),
                                 ("Administrator", module.SITE, True)):
            self.frappe.session.user, self.frappe.local.site = user, site
            self.db.auto_commit_on_many_writes = auto
            with self.subTest(user=user, site=site, auto=auto):
                self._assert_refused()
        self.frappe.get_doc.assert_not_called()
        self.db.sql.assert_not_called()

    def test_invalid_or_stale_preview_token_is_rejected_before_cancellation(self):
        for token in (None, "", "abc", "A" * 64, 123, "a" * 64):
            with self.subTest(token=token):
                self._assert_refused(token)
        token = self._token()
        self.mr.modified = "2026-09-29 16:00:00"
        self._assert_refused(token)
        self._reset_documents()
        token = self._token()
        self.bins[module.RAW]["actual_qty"] = Decimal("1.6")
        self._assert_refused(token)

    def test_exact_header_pending_purchase_scope_and_zero_fulfillment_required(self):
        changes = (("name", "MAT-MR-OTHER"), ("company", "4S Industries Limited"),
                   ("material_request_type", "Manufacture"), ("work_order", "MFG-WO-OTHER"),
                   ("docstatus", 0), ("status", "Ordered"), ("owner", "cashier"),
                   ("creation", "2026-09-29 10:34:53"), ("transaction_date", "2026-09-30"),
                   ("schedule_date", "2026-09-30"), ("amended_from", "MR-OTHER"), ("job_card", "JC-1"),
                   ("customer", "Other"), ("auto_created_via_reorder", 1), ("workflow_state", "Approved"),
                   ("set_from_warehouse", "Other"), ("set_warehouse", module.WAREHOUSE),
                   ("per_ordered", 1), ("per_received", 1), ("transfer_status", "Completed"))
        for field, value in changes:
            self._reset_documents()
            setattr(self.mr, field, value)
            with self.subTest(field=field):
                self._assert_refused()

    def test_native_validation_or_link_ignore_flags_are_never_accepted(self):
        for flag in ("ignore_permissions", "ignore_validate", "ignore_links", "ignore_mandatory", "ignore_cancel",
                     "ignore_validate_update_after_submit", "ignore_version"):
            self._reset_documents()
            setattr(self.mr.flags, flag, True)
            with self.subTest(flag=flag):
                self._assert_refused()
        self._reset_documents()
        self.mr.ignore_linked_doctypes = ["Stock Entry"]
        self._assert_refused()

    def test_exact_two_child_rows_and_no_purchasing_or_production_usage_required(self):
        changes = (("name", "OTHER"), ("idx", 2), ("parent", "MR-OTHER"), ("parenttype", "Other"),
                   ("parentfield", "other"), ("docstatus", 0), ("item_code", "OTHER"),
                   ("warehouse", "Other"), ("from_warehouse", "Other"), ("uom", "Nos"),
                   ("stock_uom", "Nos"), ("qty", 0.8), ("stock_qty", 0.8), ("conversion_factor", 2),
                   ("bom_no", "BOM-OTHER"), ("schedule_date", "2026-09-30"),
                   ("ordered_qty", 0.1), ("received_qty", 0.1), ("picked_qty", 0.1), ("rate", 1),
                   ("price_list_rate", 1), ("amount", 1), ("sales_order", "SO-1"), ("sales_order_item", "SOD-1"),
                   ("packed_item", "PI-1"), ("production_plan", "PP-1"), ("material_request_plan_item", "MRPI-1"),
                   ("job_card_item", "JCI-1"), ("project", "PROJECT-1"), ("wip_composite_asset", "ASSET-1"))
        for field, value in changes:
            self._reset_documents()
            setattr(self.mr.items[0], field, value)
            with self.subTest(field=field):
                self._assert_refused()
        self._reset_documents()
        self.mr.items.append(deepcopy(self.mr.items[0]))
        self._assert_refused()
        self._reset_documents()
        self.mr.items.reverse()
        self._assert_refused()

    def test_source_wo_and_stock_master_verification_fail_closed(self):
        for target, field, invalid in (("sale", "name", 2128), ("sale", "sale_uuid", "other"),
                                       ("sale", "sales_invoice", "SI-OTHER"), ("invoice", "name", "SI-OTHER"),
                                       ("invoice", "company", "Vox Lounge"), ("warehouse", "company", "Other"),
                                       ("warehouse", "disabled", 1), ("warehouse", "is_group", 1)):
            self._reset_documents()
            setattr(getattr(self, target), field, invalid)
            with self.subTest(target=target, field=field):
                self._assert_refused()
        for field, invalid in (("is_stock_item", 0), ("stock_uom", "Nos")):
            self._reset_documents()
            setattr(self.items[module.RAW], field, invalid)
            self._assert_refused()
        self._reset_documents()
        self.wo_validation.side_effect = production.ProductionError(production.CHANGED)
        self._assert_refused()

    def test_native_static_or_dynamic_downstream_link_prevents_cancel(self):
        for method in (self.native_static, self.native_dynamic):
            method.return_value = [{"reference_doctype": "Purchase Order", "reference_docname": "PO-1"}]
            self._assert_refused()
            method.return_value = []
        self.mr.check_no_back_links_exist.side_effect = module.RequestCancellationError("native link")
        self._assert_refused()

    def test_current_static_recheck_catches_stale_native_snapshot_and_orphan_child_reference(self):
        for doctype in ("Purchase Order Item", "Purchase Receipt Item", "Purchase Invoice Item", "Stock Entry Detail"):
            self._reset_documents()
            self.activity["`tab" + doctype + "`"] = [("LINKED-ROW",)]
            with self.subTest(doctype=doctype):
                self._assert_refused()
        queries = [call.args[0] for call in self.db.sql.call_args_list]
        self.assertTrue(any("material_request_item IN %s" in query for query in queries))
        self.assertTrue(all("FOR UPDATE" in query for query in queries))

    def test_current_dynamic_and_singleton_dependencies_are_checked(self):
        self.dynamic_metadata.return_value = {"Material Request": [Doc(parent="Payment Entry Reference",
            options="reference_doctype", fieldname="reference_name")]}
        self.activity["`tabPayment Entry Reference`"] = [("PAYMENT-REF",)]
        self._assert_refused()
        self.dynamic_metadata.return_value = {}
        self.static_metadata.return_value = [{"parent": "Test Singleton", "fieldname": "request", "issingle": 1}]
        self.activity = {"tabSingles": [("request",)]}
        self._assert_refused()
        self.static_metadata.return_value = []
        self.dynamic_metadata.return_value = {"Material Request": [Doc(parent="Test Singleton",
            options="reference_type", fieldname="reference_name")]}
        self._assert_refused()

    def test_stock_gl_and_reservation_history_even_cancelled_cannot_be_erased(self):
        for doctype in ("Stock Ledger Entry", "GL Entry", "Stock Reservation Entry"):
            for docstatus in (0, 1, 2):
                self._reset_documents()
                self.activity["`tab" + doctype + "`"] = [("HISTORY", docstatus)]
                with self.subTest(doctype=doctype, docstatus=docstatus):
                    self._assert_refused()

    def test_missing_or_insufficient_request_hold_prevents_cancel(self):
        for code in (module.RAW, module.PORTION):
            self._reset_documents()
            self.bins[code]["indented_qty"] = Decimal(0)
            self._assert_refused()
        self._reset_documents()
        del self.bins[module.RAW]
        self._assert_refused()

    def test_postcondition_rejects_every_wrong_bin_delta_and_requires_rollback(self):
        for code in (module.RAW, module.PORTION):
            for field in module.BIN_FIELDS:
                self._reset_documents()
                token = self._token()
                def wrong_delta(code=code, field=field):
                    self._native_cancel()
                    self.bins[code][field] += 1
                self.mr.cancel.side_effect = wrong_delta
                with self.subTest(code=code, field=field), self.assertRaises(module.RequestCancellationError):
                    module.cancel_request(token)
                self.db.commit.assert_not_called()
                self.assertIsNone(production._WRITING.get())

    def test_postcondition_preserves_request_business_records_and_related_documents(self):
        for target, field, value in (("mr", "title", "Rewritten history"), ("mr", "work_order", "OTHER"),
                                     ("sale", "final_total", 54000), ("invoice", "docstatus", 1),
                                     ("wo", "status", "Cancelled")):
            self._reset_documents()
            token = self._token()
            def changed_record(target=target, field=field, value=value):
                self._native_cancel()
                setattr(getattr(self, target), field, value)
            self.mr.cancel.side_effect = changed_record
            with self.subTest(target=target, field=field), self.assertRaises(module.RequestCancellationError):
                module.cancel_request(token)
            self.db.commit.assert_not_called()

    def test_postcondition_forbids_new_stock_or_accounting_activity(self):
        token = self._token()
        def unexpected_posting():
            self._native_cancel()
            self.activity["`tabStock Entry Detail`"] = [("UNEXPECTED-ENTRY",)]
        self.mr.cancel.side_effect = unexpected_posting
        with self.assertRaises(module.RequestCancellationError):
            module.cancel_request(token)
        self.db.commit.assert_not_called()

    def test_commit_attempts_are_blocked_and_real_frappe_hook_depth_is_preserved(self):
        token = self._token()
        commit = self.db.commit
        self.db._disable_transaction_control = 3
        self.mr.cancel.side_effect = lambda: self.db.commit()
        with self.assertRaises(production.ProductionError):
            module.cancel_request(token)
        self.assertIs(self.db.commit, commit)
        self.assertEqual(self.db._disable_transaction_control, 3)
        self.assertIsNone(production._WRITING.get())
        commit.assert_not_called()

    def test_apply_uses_opening_sale_invoice_inventory_wo_mr_lock_order(self):
        token = self._token()
        events = []
        self.source_lock.side_effect = lambda _: events.append("sale_opening") or self.sale
        self.inventory_lock.side_effect = lambda: events.append("inventory")
        def read(doctype, name, **kwargs):
            events.append(doctype)
            self.assertTrue(kwargs.get("for_update"))
            return self._get_doc(doctype, name, **kwargs)
        self.frappe.get_doc.side_effect = read
        self.db.sql.reset_mock()
        module.cancel_request(token)
        self.assertEqual(events[:5], ["sale_opening", "Sales Invoice", "inventory", "Work Order", "Material Request"])
        self.assertTrue(all("FOR UPDATE" in call.args[0] for call in self.db.sql.call_args_list))

    def test_cancelled_noop_still_rejects_modified_usage_or_wrong_request(self):
        token = self._token()
        module.cancel_request(token)
        self.mr.cancel.reset_mock()
        self.mr.items[0].received_qty = Decimal("0.4")
        self._assert_refused(token)
        self.mr.items[0].received_qty = 0
        self.mr.name = "MAT-MR-OTHER"
        self._assert_refused(token)
