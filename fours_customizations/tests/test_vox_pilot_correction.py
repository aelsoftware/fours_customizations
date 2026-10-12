"""No-site pilot-correction tests with native tombstone validation.

Only in-memory documents are saved. Real auto_held_lines.prepare_update and
its append-only/stale-payload guard run against a synthetic fixture. Native
invoice/modifier and retirement integration belongs to the root rollback probe.
"""

from copy import deepcopy
from datetime import datetime, time
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest import TestCase
from unittest.mock import patch


class ApiError(Exception):
    def __init__(self, status, body):
        self.status, self.body = status, body
        super().__init__(body["error"]["code"])


class Record(dict):
    def __getattr__(self, name):
        return self.get(name)

    def __setattr__(self, name, value):
        self[name] = value

    def as_dict(self):
        return deepcopy(dict(self))


class Document(Record):
    def __init__(self, fixture, values):
        super().__init__(deepcopy(values))
        object.__setattr__(self, "fixture", fixture)
        object.__setattr__(self, "flags", Record())

    def set(self, name, value):
        self[name] = value

    def append(self, name, value):
        row = Record(value)
        row.setdefault("idx", len(self[name]) + 1)
        self[name].append(row)
        return row

    def get_doc_before_save(self):
        return self.fixture.get_doc(self.doctype, str(self.name))

    def save(self):
        if self.doctype == "Kit POS Sale":
            self.fixture.auto_held.validate_tombstone_write(self)
            self.fixture.stored_ledgers.append(self.get(self.fixture.module.TOMBSTONES))
        if self.doctype == "Sales Invoice":
            for row in self.get("items"):
                row.stock_uom = row.uom
                row.stock_qty = row.qty * row.conversion_factor
                row.amount = row.qty * row.rate
                if row.item_code == self.fixture.module.NEW_ITEM:
                    row.warehouse = self.fixture.module.KITCHEN
            if len({row.warehouse for row in self.get("items")}) > 1:
                self.set_warehouse = None
            total = sum(row.amount for row in self.get("items"))
            self.grand_total = self.outstanding_amount = total
            self.amount_eligible_for_commission = total
        if self.fixture.on_save:
            self.fixture.on_save(self)
        self.fixture.calls.append(("save", self.doctype))
        self.fixture.records[(self.doctype, str(self.name))] = self.as_dict()
        return self


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class TestVoxPilotCorrection(TestCase):
    def setUp(self):
        self.records, self.calls, self.stored_ledgers = {}, [], []
        self.new_line_id, self.on_save, self.retirement_error = None, None, None
        self.denied, self.policy_error = set(), None
        self.effects = {}
        self.frappe = ModuleType("frappe")
        self.frappe._dict = Record
        self.frappe.local = SimpleNamespace(site="multax.kit.africa")
        self.frappe.session = SimpleNamespace(user="Administrator")
        self.frappe.PermissionError = PermissionError
        self.frappe.get_doc = self.get_doc
        self.frappe.throw = lambda message, error=ValueError: self.raise_error(error, message)
        self.frappe.enqueue = lambda *args, **kwargs: self.calls.append(("enqueue",))
        self.frappe.db = SimpleNamespace(
            auto_commit_on_many_writes=False, _disable_transaction_control=False,
            has_column=lambda *args: True,
            commit=lambda: self.calls.append(("commit",)),
            get_value=lambda doctype, name, field: self.records[(doctype, str(name))].get(field),
        )
        utils = ModuleType("frappe.utils")
        utils.cint = lambda value: int(value or 0)
        utils.get_datetime = lambda value: value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        utils.getdate = lambda value: datetime.fromisoformat(str(value)).date()
        utils.get_time = lambda value: value if isinstance(value, time) else time.fromisoformat(str(value))
        connector = ModuleType("kit_pos.connector")
        connector.__path__ = []
        kit = ModuleType("kit_pos")
        kit.__path__ = []
        router = ModuleType("kit_pos.connector.router")
        router.ApiError = ApiError
        audit = ModuleType("kit_pos.connector.line_removal_audit")
        audit.FIELD = "line_removal_approvals"
        audit.validate_stored_approval = lambda *args: None
        audit.prepare_approvals = self.prepare_approvals
        self.sales = ModuleType("kit_pos.connector.sales")
        self.sales._effective_location_settings = lambda *args, **kwargs: {"auto_hold_sales": 0}
        self.sales.merge_line_quantity = lambda old, row: float(row["quantity"]) if "base_quantity" not in row else float(old) + float(row["quantity"]) - float(row["base_quantity"])
        self.sales._authorize_sale_payload = lambda *args, **kwargs: self.calls.append(("authorize_payload",))
        self.sales.remove_lines = self.remove_lines
        self.sales.sync_lines = self.sync_lines
        self.sales.settle_money = self.settle_money
        self.sales.serialize_pulled = lambda sale: {**json.loads(sale.payload), "id": int(sale.name)}
        permissions = ModuleType("kit_pos.connector.sale_permissions")
        permissions.PosAccess = self.access
        permissions.validate_saved_creator = lambda sale, payload: self.assertEqual(payload["created_by"], sale.created_by_id)
        policy = ModuleType("kit_pos.connector.item_policy")
        policy.validate_sale_items = lambda sale: None
        self.materialize = ModuleType("kit_pos.connector.materialize")
        self.materialize._customer_for = lambda sale: sale.customer
        self.materialize._pos_profile_for = lambda location: location["pos_profile"]
        self.materialize._posting = lambda sale: {"posting_date": "2026-09-28", "posting_time": "23:09:40", "set_posting_time": 1}
        self.materialize._invoice_items = self.invoice_items
        self.materialize._bind_exact_opening_validation = lambda *args: True
        self.vox = ModuleType("fours_customizations.vox_modifiers")
        self.vox.validate_invoice_modifiers = self.validate_modifiers
        self.vox.invoice_options = lambda invoice, row: [{"public_id": 3998074021553850, "group_name": "Sides", "label": "Chips", "price": "10000", "stock_policy": "sales_only"}]
        self.work_orders = ModuleType("fours_customizations.vox_work_orders")
        self.work_orders._retire_legacy_pilot_work_order = self.retire
        modules = {
            "frappe": self.frappe, "frappe.utils": utils, "kit_pos": kit,
            "kit_pos.connector": connector, "kit_pos.connector.router": router,
            "kit_pos.connector.line_removal_audit": audit, "kit_pos.connector.sales": self.sales,
            "kit_pos.connector.sale_permissions": permissions, "kit_pos.connector.item_policy": policy,
            "kit_pos.connector.materialize": self.materialize,
            "fours_customizations.vox_modifiers": self.vox,
            "fours_customizations.vox_work_orders": self.work_orders,
        }
        self.enterContext(patch.dict(sys.modules, modules))
        import fours_customizations
        self.enterContext(patch.object(fours_customizations, "vox_modifiers", self.vox, create=True))
        self.enterContext(patch.object(fours_customizations, "vox_work_orders", self.work_orders, create=True))
        root = Path(__file__).resolve().parents[1]
        self.module = load_module("fours_customizations._isolated_vox_pilot_correction", root / "vox_pilot_correction.py")
        native_path = root.parents[1] / "kit_pos" / "kit_pos" / "connector" / "auto_held_lines.py"
        self.auto_held = load_module("kit_pos.connector.auto_held_lines", native_path)
        self.addCleanup(sys.modules.pop, "fours_customizations._isolated_vox_pilot_correction", None)
        self.addCleanup(sys.modules.pop, "kit_pos.connector.auto_held_lines", None)
        connector.auto_held_lines = self.auto_held
        self.seed()
        self.enterContext(patch.object(self.module, "_read_state", side_effect=self.read_state))

    @staticmethod
    def raise_error(error, message):
        raise error(message)

    def access(self):
        def require(permission, *args):
            self.calls.append(("permission", permission))
            if permission in self.denied:
                raise PermissionError(permission)
        return SimpleNamespace(user="Administrator", require=require,
            require_sale=lambda sale, name: self.assertEqual(str(sale.name), name),
            require_location=lambda location: self.assertEqual(location, 1996))

    def get_doc(self, doctype, name, **kwargs):
        return Document(self, self.records[(doctype, str(name))])

    def seed(self):
        m = self.module
        self.location = Record(name=m.LOCATION, company=m.COMPANY, warehouse=m.BAR_WAREHOUSE, pos_profile=m.LOCATION)
        rows, invoice_rows, products = [], [], []
        values = [(m.OLD_LINE_ID, m.OLD_LINE_UUID, m.OLD_ITEM, 554, 1, 25000), *m.RETAINED]
        for index, (line_id, uuid, code, product, qty, rate) in enumerate(values, 1):
            payload = {"desktop_line_uuid": uuid, "desktop_line_no": "OLD-LINE-" + str(line_id),
                       "product_id": product, "variation_id": product, "quantity": qty,
                       "unit_price": rate, "unit_price_before_discount": rate, "unit_price_inc_tax": rate,
                       "sub_unit_id": 1254, "unit_name": "Pc(s)", "base_unit_multiplier": 1,
                       "kot_printed_at": "2026-09-28 23:09:40"}
            products.append(payload)
            rows.append(Record(payload, name="OLD-" + str(line_id), idx=index, parent=m.SALE,
                line_id=line_id, item_code=code, item_tax=0, line_discount_amount=0,
                parent_sell_line_id=0, children_type=None, line_payload=json.dumps(payload)))
            invoice_rows.append(Record(name="SII-" + str(index), idx=index, item_code=code, qty=qty,
                rate=rate, amount=qty * rate, stock_uom="Pc(s)", uom="Pc(s)", stock_qty=qty,
                conversion_factor=1, warehouse=m.BAR_WAREHOUSE))
        payload = {"uuid": m.SALE_UUID, "invoice_no": m.LABEL, "transaction_date": str(m.SOLD_AT),
            "location_id": 1996, "cash_register_id": m.OPENING, "created_by": 2210,
            "service_staff_id": 2051, "table_id": 2136, "contact_id": 1265,
            "status": "final", "is_suspend": 1, "final_total": 49000, "payments": [],
            "products": products, "removed_line_ids": [], "removed_line_uuids": [], "sale_note": "Keep original note"}
        self.records[("Kit POS Sale", m.SALE)] = dict(doctype="Kit POS Sale", name=int(m.SALE), sale_uuid=m.SALE_UUID,
            invoice_no=m.LABEL, transaction_date=m.SOLD_AT, location_id=1996, location=m.LOCATION,
            pos_opening_entry=m.OPENING, created_by_id=2210, waiter_id=2051, table_id=2136, contact_id=1265,
            customer="Walk-In Customer", status="final", is_suspend=1, is_quotation=0, payment_status="due",
            amount_paid=0, payment_revision=0, materialize_state="draft", sales_invoice=m.INVOICE,
            final_total=49000, payments=[], lines=rows, payload=json.dumps(payload), removed_line_tombstones=None)
        self.records[("Sales Invoice", m.INVOICE)] = dict(doctype="Sales Invoice", name=m.INVOICE, docstatus=0,
            status="Draft", company=m.COMPANY, customer="Walk-In Customer", pos_profile=m.LOCATION,
            set_warehouse=m.BAR_WAREHOUSE, posting_date="2026-09-28", posting_time="23:09:40", set_posting_time=1,
            is_pos=1, is_created_using_pos=1, update_stock=1, currency="UGX", conversion_rate=1,
            grand_total=49000, outstanding_amount=49000, amount_eligible_for_commission=49000,
            paid_amount=0, payments=[], advances=[], items=invoice_rows)
        self.records[("Work Order", m.PLANNING_WORK_ORDER)] = dict(doctype="Work Order", name=m.PLANNING_WORK_ORDER,
            docstatus=1, status="Not Started", custom_vox_production_key="ORIGINAL-IMMUTABLE-KEY")

    def read_state(self, *, locked=False):
        m = self.module
        if locked:
            self.calls.append(("lock", "opening-sale-invoice"))
        return {"sale": self.get_doc("Kit POS Sale", m.SALE), "invoice": self.get_doc("Sales Invoice", m.INVOICE),
            "work_order": self.get_doc("Work Order", m.PLANNING_WORK_ORDER), "location": self.location,
            "setup": {"product_id": 15841, "uom_id": 1254, "modifier_id": 3998074021553850,
                "new_line_id": self.new_line_id, "new_production_key": "NEW-IMMUTABLE-KEY",
                "item": {"name": m.NEW_ITEM, "is_stock_item": 1}, "modifier": {"name": "Chips", "price": 10000}},
            "effects": self.effects}

    def prepare_approvals(self, sale, payload, access, ledger, removals, unseen, stamp):
        self.calls.append(("native_removal_policy",))
        self.assertNotIn("line_removal_approvals", payload)
        if self.policy_error:
            raise self.policy_error
        return {}

    def retire(self):
        self.calls.append(("retire",))
        if self.retirement_error:
            raise self.retirement_error
        m = self.module
        wo = self.records[("Work Order", m.PLANNING_WORK_ORDER)]
        already = wo["docstatus"] == 2
        if not already:
            self.assertEqual(self.records[("Kit POS Sale", m.SALE)]["final_total"], 49000)
            wo.update(docstatus=2, status="Cancelled")
        return {"work_order": m.PLANNING_WORK_ORDER, "already_cancelled": already,
                "raw_reservation_released": "0" if already else "0.4",
                "finished_planned_quantity_released": "0" if already else "1"}

    def remove_lines(self, sale, uuids, ids):
        self.calls.append(("remove", tuple(ids)))
        sale.lines = [row for row in sale.lines if row.desktop_line_uuid not in uuids and row.line_id not in ids]

    def sync_lines(self, sale, products):
        self.calls.append(("sync", len(products)))
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["desktop_line_uuid"], self.module.NEW_LINE_UUID)
        self.auto_held.protect_line_merge(sale, products)
        self.new_line_id = 99901
        product = products[0]
        proof = {**deepcopy(product), "_kit_pos_modifiers": {"version": 1, "reference": "TEST-NATIVE-SELECTION"}}
        sale.lines.append(Record(product, name="NEW-NATIVE-CHILD", line_id=self.new_line_id,
            item_code=self.module.NEW_ITEM, idx=5, parent=self.module.SALE, parent_sell_line_id=0,
            children_type=None, line_payload=json.dumps(proof)))

    @staticmethod
    def settle_money(sale):
        assert not sale.payments
        sale.amount_paid, sale.payment_status = 0, "due"

    def invoice_items(self, sale, location):
        return [{"item_code": row.item_code, "qty": row.quantity, "rate": 30000 if index == 0 else row.unit_price_inc_tax,
            "uom": "Pc(s)", "conversion_factor": 1, "warehouse": location.warehouse,
            **({self.module.MODIFIER_FIELD: "TEST-NATIVE-SELECTION"} if index == 0 else {})}
            for index, row in enumerate(sale.lines)]

    def validate_modifiers(self, invoice):
        self.assertEqual(invoice.get("items")[0].get(self.module.MODIFIER_FIELD), "TEST-NATIVE-SELECTION")

    def apply(self):
        return self.module.apply_correction(expected_digest=self.module.preview()["before_digest"])

    def test_preview_is_read_only_and_has_stable_deterministic_identity(self):
        before = deepcopy(self.records)
        one, two = self.module.preview(), self.module.preview()
        self.assertEqual(one, two)
        self.assertEqual(one["state"], "ready")
        self.assertEqual(one["new_line_uuid"], "c62b2215-2be3-5fe2-be12-9be63e6199e5")
        self.assertEqual(self.records, before)
        self.assertIsNone(self.new_line_id)
        self.assertFalse(any(call[0] in ("save", "retire", "commit", "enqueue") for call in self.calls))

    def test_mapping_with_none_as_dict_attribute_is_copied_without_calling_it(self):
        class NativeMapping(dict):
            def __getattr__(self, key):
                return self.get(key)
        original = NativeMapping(name="4929", lines=[{"quantity": 1}])
        copied = self.module._dict(original)
        self.assertEqual(copied, original)
        copied["lines"][0]["quantity"] = 2
        self.assertEqual(original["lines"][0]["quantity"], 1)

    def test_exact_correction_keeps_held_unpaid_draft_and_retained_rows(self):
        before = deepcopy(self.records)
        result = self.apply()
        self.assertEqual(result["state"], "corrected")
        sale = self.records[("Kit POS Sale", self.module.SALE)]
        invoice = self.records[("Sales Invoice", self.module.INVOICE)]
        self.assertEqual((sale["final_total"], invoice["grand_total"], invoice["outstanding_amount"]), (54000, 54000, 54000))
        self.assertEqual((sale["is_suspend"], sale["payment_status"], sale["amount_paid"], sale["payment_revision"]), (1, "due", 0, 0))
        self.assertEqual((invoice["docstatus"], invoice["paid_amount"], invoice["payments"]), (0, 0, []))
        self.assertIsNone(invoice["set_warehouse"])
        self.assertEqual(sale["lines"][1:], before[("Kit POS Sale", self.module.SALE)]["lines"][1:])
        self.assertEqual(sale["lines"][0]["idx"], 1)
        self.assertIsNone(sale["lines"][0]["kot_printed_at"])
        self.assertEqual(self.calls.count(("sync", 1)), 1)
        self.assertLess(self.calls.index(("retire",)), self.calls.index(("remove", (15806,))))
        self.assertFalse(any(call[0] in ("commit", "enqueue") for call in self.calls))

    def test_only_real_entry_is_staged_no_empty_ledger_or_fake_pin_approval(self):
        self.apply()
        self.assertEqual(len(self.stored_ledgers), 1)
        ledger = json.loads(self.stored_ledgers[0])
        self.assertEqual((ledger["revision"], len(ledger["entries"])), (1, 1))
        entry = ledger["entries"][0]
        self.assertEqual((entry["line_id"], entry["desktop_line_uuid"], entry["removed_by"]),
                         (15806, self.module.OLD_LINE_UUID, "Administrator"))
        self.assertNotIn("approval", entry)
        self.assertEqual(self.calls.count(("native_removal_policy",)), 1)

    def test_existing_auto_hold_off_still_requires_explicit_management_removal(self):
        self.denied.add("sell.remove_line_items")
        before = deepcopy(self.records)
        with self.assertRaises(PermissionError):
            self.apply()
        self.assertEqual(self.records, before)

    def test_changed_preview_hash_prevents_cancellation_or_save(self):
        with self.assertRaisesRegex(self.module.CorrectionError, "Preview changed"):
            self.module.apply_correction(expected_digest="unreviewed")
        self.assertFalse(any(call[0] in ("save", "retire") for call in self.calls))

    def test_legitimate_material_request_backlink_failure_changes_no_bill(self):
        before = deepcopy(self.records)
        self.retirement_error = ValueError("Submitted Material Request still links this Work Order")
        with self.assertRaisesRegex(ValueError, "Material Request"):
            self.apply()
        self.assertEqual(self.records, before)
        self.assertEqual(self.stored_ledgers, [])
        self.assertFalse(any(call[0] in ("remove", "sync", "save") for call in self.calls))

    def test_optional_native_removal_policy_can_reject_before_retirement(self):
        self.policy_error = PermissionError("Policy requires reviewed authority")
        before = deepcopy(self.records)
        with self.assertRaises(PermissionError):
            self.apply()
        self.assertEqual(self.records, before)
        self.assertNotIn(("retire",), self.calls)

    def test_exact_repeat_is_no_op_after_verifying_cancellation(self):
        self.apply()
        before, calls = deepcopy(self.records), len(self.calls)
        result = self.apply()
        self.assertEqual(result["state"], "already_corrected")
        self.assertEqual(self.records, before)
        self.assertIn(("retire",), self.calls[calls:])
        self.assertFalse(any(call[0] in ("sync", "remove", "save") for call in self.calls[calls:]))

    def test_corrected_state_without_cancellation_is_not_a_no_op(self):
        self.apply()
        self.records[("Work Order", self.module.PLANNING_WORK_ORDER)]["docstatus"] = 1
        with self.assertRaises(self.module.CorrectionError):
            self.module.preview()

    def test_old_desktop_line_gets_native_stale_error_with_current_sale(self):
        original = json.loads(self.records[("Kit POS Sale", self.module.SALE)]["payload"])
        self.apply()
        sale = self.get_doc("Kit POS Sale", self.module.SALE)
        with self.assertRaises(ApiError) as raised:
            self.auto_held.prepare_update(sale, original, self.access())
        self.assertEqual((raised.exception.status, raised.exception.body["error"]["code"]), (409, "AUTO_HELD_LINES_STALE"))
        current = raised.exception.body["error"]["details"]["current_sale"]
        self.assertEqual((current["uuid"], current["final_total"]), (self.module.SALE_UUID, 54000))
        self.assertEqual(current["products"][0]["desktop_line_uuid"], self.module.NEW_LINE_UUID)

    def test_tombstone_missing_actor_or_wrong_original_id_rejects_no_op(self):
        self.apply()
        sale = self.records[("Kit POS Sale", self.module.SALE)]
        original = sale[self.module.TOMBSTONES]
        for changes in ({"line_id": 15807}, {"removed_by": "Someone Else"}):
            ledger = json.loads(original)
            ledger["entries"][0].update(changes)
            sale[self.module.TOMBSTONES] = json.dumps(ledger)
            with self.subTest(changes=changes), self.assertRaises(self.module.CorrectionError):
                self.module.preview()
        sale[self.module.TOMBSTONES] = original

    def test_unconfirmed_payment_or_submitted_invoice_or_wrong_uuid_fails(self):
        for doctype, name, field, value in (
            ("Kit POS Sale", self.module.SALE, "sale_uuid", "different"),
            ("Kit POS Sale", self.module.SALE, "payments", [Record(amount=1)]),
            ("Kit POS Sale", self.module.SALE, "payment_revision", 1),
            ("Sales Invoice", self.module.INVOICE, "docstatus", 1),
            ("Sales Invoice", self.module.INVOICE, "grand_total", 54000),
        ):
            row = self.records[(doctype, name)]
            old = deepcopy(row[field])
            row[field] = value
            with self.subTest(field=field), self.assertRaises(self.module.CorrectionError):
                self.module.preview()
            row[field] = old

    def test_new_production_or_separate_payment_blocks_any_correction(self):
        self.effects["replacement_work_orders"] = 1
        with self.assertRaises(self.module.CorrectionError):
            self.module.preview()
        self.assertNotIn(("retire",), self.calls)

    def test_native_unrelated_retained_row_change_is_detected(self):
        def mutate(doc):
            if doc.doctype == "Kit POS Sale":
                doc.lines[1].kot_printed_at = "different"
        self.on_save = mutate
        with self.assertRaisesRegex(self.module.CorrectionError, "retained source line"):
            self.apply()
        self.assertFalse(any(call[0] in ("commit", "enqueue") for call in self.calls))

    def test_helper_blocks_commits_and_queues_and_restores_caller_methods(self):
        for method in ("commit", "enqueue"):
            target = self.frappe.db if method == "commit" else self.frappe
            original = getattr(target, method)
            with self.subTest(method=method):
                with self.assertRaisesRegex(self.module.CorrectionError, "must not commit or enqueue"):
                    with self.module._no_commit_or_queue():
                        getattr(target, method)()
                self.assertIs(getattr(target, method), original)

    def test_only_administrator_exact_site_allowed(self):
        self.frappe.session.user = "cashier@example.test"
        with self.assertRaises(self.module.CorrectionError):
            self.module.preview()
        self.frappe.session.user = "Administrator"
        self.frappe.local.site = "other.test"
        with self.assertRaises(self.module.CorrectionError):
            self.module.preview()
