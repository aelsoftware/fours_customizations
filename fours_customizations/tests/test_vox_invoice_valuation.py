"""Write-free regressions for the scoped, synchronous finished-dish valuation.

The stock engine is replaced with an in-memory replay.  No site is initialized,
no database connection is opened, and no business documents are submitted.
"""

import sys
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from types import ModuleType, SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from fours_customizations import vox_work_orders as module

WHEN = datetime(2026, 9, 28, 23, 9, 40)
CREATED = datetime(2026, 9, 29, 1, 0)
PAN = "VXN-PAN-FRIED-GOAT"
DRY = "VXN-DRY-GOAT"
USER = "cashier@example.com"
BAR = "Main counter - VXN"


class Doc(SimpleNamespace):
    def get(self, name, default=None):
        return self.__dict__.get(name, default)

    def as_dict(self):
        def plain(value):
            if isinstance(value, Doc):
                return {key: plain(part) for key, part in vars(value).items()}
            if isinstance(value, list):
                return [plain(part) for part in value]
            if isinstance(value, dict):
                return {key: plain(part) for key, part in value.items()}
            return deepcopy(value)

        return plain(self)


class Row(dict):
    __getattr__ = dict.get
    __setattr__ = dict.__setitem__


def sle(name, *, code=PAN, quantity=1, balance=1, value=10000,
        difference=10000, voucher_type="Stock Entry", voucher_no="SE-1",
        detail="SEI-FG-1", when=WHEN, sequence=1):
    return Row(
        name=name, item_code=code,
        warehouse=module.WAREHOUSE, company=module.COMPANY, stock_uom="Pc(s)",
        voucher_type=voucher_type, voucher_no=voucher_no, voucher_detail_no=detail,
        posting_datetime=when, posting_date=when.date(), posting_time=when.time(),
        creation=CREATED + timedelta(microseconds=sequence), modified=CREATED,
        modified_by=USER, owner=USER, docstatus=1, is_cancelled=0,
        actual_qty=quantity, qty_after_transaction=balance, stock_value=value,
        stock_value_difference=difference, valuation_rate=10000,
        incoming_rate=10000 if quantity > 0 else 0, outgoing_rate=0,
        stock_queue="[[1.0, 10000.0]]" if balance == 1 else "[]",
        recalculate_rate=int(voucher_type == "Stock Entry" and quantity > 0),
        dependant_sle_voucher_detail_no=None, serial_no=None, batch_no=None,
        serial_and_batch_bundle=None, has_serial_no=0, has_batch_no=0,
        auto_created_serial_and_batch_bundle=0, is_adjustment_entry=0,
        project=None, fiscal_year="2026", to_rename=0,
    )


def bin_row(code, *, quantity=0, value=0, rate=0, warehouse=module.WAREHOUSE):
    return Row(
        name="BIN-" + code, item_code=code, warehouse=warehouse,
        company=module.COMPANY, stock_uom="Kg" if code == module.RAW else "Pc(s)",
        actual_qty=quantity, stock_value=value, valuation_rate=rate,
        projected_qty=quantity, planned_qty=0, ordered_qty=0, reserved_qty=0,
        indented_qty=0, reserved_qty_for_production=0,
        reserved_qty_for_sub_contract=0, reserved_qty_for_production_plan=0,
        reserved_stock=0, modified=CREATED, modified_by=USER,
    )


def invoice_document():
    line_uuid = module.PILOT["line_uuid"]
    return Doc(
        doctype="Sales Invoice", name="SI-1", docstatus=1, _action="submit",
        company=module.COMPANY, customer="Vox Walk-in", currency="UGX",
        conversion_rate=1, is_pos=1, is_created_using_pos=1, is_return=0,
        update_stock=1, posting_date=WHEN.date(), posting_time=WHEN.time(),
        pos_profile="Main Bar", set_warehouse=BAR, grand_total=54000,
        paid_amount=54000, outstanding_amount=0, debit_to="Debtors - VXN",
        items=[Doc(
            doctype="Sales Invoice Item", name="SII-1", parent="SI-1",
            parenttype="Sales Invoice", parentfield="items",
            idx=1, item_code=PAN, qty=1, stock_qty=1, rate=30000, amount=30000,
            base_rate=30000, base_amount=30000, net_rate=30000, net_amount=30000,
            uom="Pc(s)", stock_uom="Pc(s)", conversion_factor=1,
            warehouse=module.WAREHOUSE, incoming_rate=0, cost_center=module.COST_CENTER,
            expense_account="Cost of Goods Sold - VXN", modified=CREATED, modified_by=USER,
            **{module.KEY_FIELD: module.production_key(module.PILOT["sale_uuid"], line_uuid),
               module.WORK_ORDER_FIELD: "WO-1", module.MODIFIER_FIELD: ""},
        )],
        payments=[Doc(name="PAY-1", mode_of_payment="Cash", amount=54000)],
        taxes=[], advances=[], payment_schedule=[], sales_team=[],
        modified=CREATED, modified_by=USER,
    )


def manufacture_document():
    return Doc(
        doctype="Stock Entry", name="SE-1", docstatus=1, company=module.COMPANY,
        purpose="Manufacture", work_order="WO-1", bom_no=module.MENU[PAN],
        posting_date=WHEN.date(), posting_time=WHEN.time(), set_posting_time=1,
        fg_completed_qty=1, process_loss_qty=0, additional_costs=[],
        total_incoming_value=10000, total_outgoing_value=10000, value_difference=0,
        is_return=0, job_card=None, modified=CREATED, modified_by=USER,
        items=[
            Doc(name="SEI-RAW-1", item_code=module.RAW, qty=0.4, transfer_qty=0.4,
                uom="Kg", stock_uom="Kg", conversion_factor=1,
                s_warehouse=module.WAREHOUSE, t_warehouse=None, is_finished_item=0,
                basic_rate=25000, basic_amount=10000, valuation_rate=25000,
                amount=10000, additional_cost=0, modified=CREATED, modified_by=USER),
            Doc(name="SEI-FG-1", item_code=PAN, qty=1, transfer_qty=1,
                uom="Pc(s)", stock_uom="Pc(s)", conversion_factor=1,
                s_warehouse=None, t_warehouse=module.WAREHOUSE, is_finished_item=1,
                basic_rate=10000, basic_amount=10000, valuation_rate=10000,
                amount=10000, additional_cost=0, modified=CREATED, modified_by=USER),
        ],
    )


def state_for(invoice=None, entry=None):
    invoice = invoice or invoice_document()
    entry = entry or manufacture_document()
    stock = [
        sle("SLE-MAKE-1"),
        sle("SLE-SALE-1", quantity=-1, balance=0, value=0, difference=-10000,
            voucher_type="Sales Invoice", voucher_no="SI-1", detail="SII-1", sequence=2),
    ]
    gl = []
    for number, account, debit, credit in (
        (1, "Debtors - VXN", 54000, 0), (2, "Sales - VXN", 0, 54000),
        (3, "Cost of Goods Sold - VXN", 10000, 0), (4, "Stock In Hand - VXN", 0, 10000),
    ):
        gl.append(Row(name=f"GLE-{number}", voucher_type="Sales Invoice", voucher_no="SI-1",
                      account=account, debit=debit, credit=credit, company=module.COMPANY,
                      is_cancelled=0, docstatus=1, modified=CREATED, modified_by=USER))
    return {
        "sles": stock,
        "previous": [],
        "bins": [
            bin_row(module.RAW, quantity=1.6, value=40000, rate=25000),
            bin_row(PAN, value=10000, rate=10000), bin_row(DRY), bin_row(module.PORTION),
        ],
        "vouchers": {("Sales Invoice", invoice.name): invoice.as_dict(),
                     ("Stock Entry", entry.name): entry.as_dict()},
        "gl": gl,
    }


class TestVoxInvoiceValuation(TestCase):
    def setUp(self):
        self.db = SimpleNamespace(
            sql=Mock(return_value=[]), get_value=Mock(return_value=0),
            get_single_value=Mock(return_value=0), has_column=Mock(return_value=False),
            auto_commit_on_many_writes=False, _disable_transaction_control=2,
            commit=Mock(), rollback=Mock(), _conn=object(),
        )
        self.frappe = SimpleNamespace(
            db=self.db, local=SimpleNamespace(site=module.SITE, message_log=[]),
            session=SimpleNamespace(user=USER), conf={module.CONFIG: {module.COMPANY: True}},
            get_all=Mock(return_value=[]), get_doc=Mock(), _dict=Row,
        )
        self.enterContext(patch.object(module, "frappe", self.frappe))
        self.enterContext(patch.object(module, "now_datetime", return_value=CREATED))
        self.native = ModuleType("erpnext.stock.stock_ledger")
        self.replay = self.native.update_entries_after = Mock(side_effect=self._native_replay)
        self.native.repost_future_sle = Mock()
        self.enterContext(patch.dict(sys.modules, {"erpnext.stock.stock_ledger": self.native}))
        self.invoice = invoice_document()
        self.entry = manufacture_document()
        self.state = state_for(self.invoice, self.entry)
        self.sale = Doc(name="4929", sale_uuid=module.PILOT["sale_uuid"])
        self.location = Doc(name="Main Bar", company=module.COMPANY, warehouse=BAR)
        self.pairs = [(self.invoice.items[0], Doc(
            line_id="15806", desktop_line_uuid=module.PILOT["line_uuid"],
        ))]
        self.work_orders = {module.PILOT["line_uuid"]: Doc(name="WO-1", status="Completed")}
        self.entries = {"WO-1": self.entry}
        self.schema = self.enterContext(patch.object(module, "_require_schema"))
        self.trusted = self.enterContext(patch.object(
            module, "_trusted_source", return_value=(self.sale, self.location),
        ))
        self.lock = self.enterContext(patch.object(module, "_lock_inventory"))
        self.matches = self.enterContext(patch.object(
            module, "_matched_lines", side_effect=lambda *args: self.pairs,
        ))
        self.plan = self.enterContext(patch.object(module, "_plan", side_effect=self._plan))
        self.existing = self.enterContext(patch.object(
            module, "_existing", side_effect=lambda plan: self.work_orders.get(plan["line_uuid"]),
        ))
        self.lineage = self.enterContext(patch.object(module, "_assert_lineage"))
        self.completed = self.enterContext(patch.object(module, "_validate_work_order"))
        self.manufacture = self.enterContext(patch.object(
            module, "_manufacture_for", side_effect=lambda wo, plan: self.entries[wo.name],
        ))
        self.real_snapshot = module._valuation_snapshot
        self.snapshot = self.enterContext(patch.object(
            module, "_valuation_snapshot", side_effect=lambda *args: deepcopy(self.state),
        ))
        self.db.sql.side_effect = self._bin_sql

    def _plan(self, invoice, sale, row, line):
        return {
            "sale_uuid": self.sale.sale_uuid, "line_uuid": line.desktop_line_uuid,
            "production_item": row.item_code, "quantity": str(row.qty),
            "raw_quantity": str(Decimal(str(row.qty)) * module.PER_PORTION),
            "posting_datetime": str(WHEN), "recipe": {"bom": module.MENU[row.item_code]},
        }

    def _bin_sql(self, query, params=None, **kwargs):
        self.assertIn("FOR UPDATE", query)
        self.assertIn("SELECT actual_qty,stock_value,valuation_rate FROM tabBin", query)
        code, warehouse = params
        return deepcopy([row for row in self.state["bins"]
                         if row.item_code == code and row.warehouse == warehouse])

    def _finalize(self):
        return module.finalize_invoice_valuation(self.invoice, "on_submit")

    def _refused(self):
        with self.assertRaises(module.ProductionError):
            self._finalize()
        self.db.commit.assert_not_called()
        self.db.rollback.assert_not_called()
        self.assertIsNone(module._WRITING.get())

    def _bin(self, code=PAN):
        return next(row for row in self.state["bins"] if row.item_code == code)

    def _snapshot_sql(self, query, params=None, **kwargs):
        self.assertIn("FOR UPDATE", query)
        if "`tabStock Ledger Entry`" in query:
            if "posting_datetime>=" in query:
                return deepcopy(self.state["sles"])
            self.assertIn("posting_datetime<", query)
            return deepcopy([row for row in self.state["previous"] if row.item_code == params[0]])
        if "`tabGL Entry`" in query:
            return deepcopy(self.state["gl"])
        if "SELECT * FROM tabBin" in query:
            return deepcopy(self.state["bins"])
        raise AssertionError(query)

    def _read_snapshot(self):
        self.db.sql.side_effect = self._snapshot_sql
        self.frappe.get_doc.side_effect = lambda doctype, name, **kwargs: Doc(
            **deepcopy(self.state["vouchers"][(doctype, name)]),
        )
        return self.real_snapshot((PAN,), WHEN)

    def _native_replay(self, args, **kwargs):
        self.assertNotIn("sle_id", args)
        self.assertNotIn("repost_doc", args)
        self.assertFalse(kwargs.get("allow_negative_stock", False))
        self.assertIsNotNone(module._WRITING.get())
        self.assertEqual(self.db._disable_transaction_control, 2)
        code = args["item_code"]
        values = [row for row in self.state["sles"] if row.item_code == code]
        latest = max(values, key=lambda row: (row.posting_datetime, row.creation))
        for row in self.state["bins"]:
            if row.item_code == code and row.warehouse == module.WAREHOUSE:
                row.actual_qty = latest.qty_after_transaction
                row.stock_value = latest.stock_value
                row.valuation_rate = latest.valuation_rate
                row.modified = CREATED + timedelta(seconds=1)
                row.modified_by = USER
        for row in values:
            row.modified = CREATED + timedelta(seconds=1)
            if row.voucher_type == "Sales Invoice":
                invoice = self.state["vouchers"][(row.voucher_type, row.voucher_no)]
                for item in invoice["items"]:
                    if item["name"] == row.voucher_detail_no:
                        item["incoming_rate"] = abs(row.stock_value_difference / row.actual_qty)
                        item["modified"] = CREATED + timedelta(seconds=1)
                        item["modified_by"] = USER
        return SimpleNamespace(repost_affected_transaction=set())

    def test_tied_manufacture_and_sale_refresh_only_finished_bin_value(self):
        raw_before = deepcopy(self._bin(module.RAW))
        gl_before = deepcopy(self.state["gl"])
        self.assertEqual(self._finalize(), [PAN])
        self.assertEqual((self._bin().actual_qty, self._bin().stock_value), (0, 0))
        self.assertEqual(self._bin().valuation_rate, 10000)
        self.assertEqual(self._bin(module.RAW), raw_before)
        self.assertEqual(self.state["gl"], gl_before)
        self.trusted.assert_called_once_with(self.invoice)
        self.completed.assert_called_once()
        self.assertTrue(self.completed.call_args.kwargs["completed"])
        self.lineage.assert_called_once()
        self.manufacture.assert_called_once()
        self.snapshot.assert_any_call((PAN,), WHEN)
        self.replay.assert_called_once_with(
            {"item_code": PAN, "warehouse": module.WAREHOUSE,
             "posting_date": WHEN.date(), "posting_time": WHEN.time()},
            allow_negative_stock=False,
        )
        self.native.repost_future_sle.assert_not_called()
        self.db.commit.assert_not_called()
        self.db.rollback.assert_not_called()
        self.assertIsNone(module._WRITING.get())

    def test_repeated_finalizer_does_not_recook_or_change_accounting(self):
        self._finalize()
        before = deepcopy(self.state)
        self._finalize()
        self.assertEqual(self.state, before)
        self.assertEqual(self.replay.call_count, 2)
        self.db.commit.assert_not_called()

    def test_feature_scope_is_site_company_and_explicit_opt_in(self):
        cases = (
            (module.SITE, "4S Industries Limited", self.frappe.conf),
            ("ddamu.kit.africa", module.COMPANY, self.frappe.conf),
            (module.SITE, module.COMPANY, {}),
            (module.SITE, module.COMPANY, {module.CONFIG: {module.COMPANY: False}}),
        )
        for site, company, config in cases:
            with self.subTest(site=site, company=company, config=config):
                self.frappe.local.site, self.invoice.company, self.frappe.conf = site, company, config
                self.assertIsNone(self._finalize())
        self.schema.assert_not_called()
        self.trusted.assert_not_called()
        self.snapshot.assert_not_called()
        self.replay.assert_not_called()

    def test_unrelated_drinks_and_legacy_shared_dish_do_not_replay(self):
        for code in ("VX-0017", "VX-0128"):
            with self.subTest(code=code):
                self.invoice.items = [Doc(item_code=code, warehouse=BAR)]
                self.assertIsNone(self._finalize())
        self.schema.assert_not_called()
        self.replay.assert_not_called()

    def test_only_on_submit_action_and_submitted_sales_invoice_are_accepted(self):
        original = self.invoice
        for field, value in (("_action", "save"), ("_action", "cancel"),
                             ("docstatus", 0), ("docstatus", 2), ("doctype", "POS Invoice")):
            with self.subTest(field=field, value=value):
                self.invoice = deepcopy(original)
                setattr(self.invoice, field, value)
                self._refused()
        self.invoice = original
        for method in (None, "before_submit", "on_update", "on_cancel"):
            with self.subTest(method=method), self.assertRaises(module.ProductionError):
                module.finalize_invoice_valuation(self.invoice, method)
        self.replay.assert_not_called()
        self.trusted.assert_not_called()

    def test_foreign_line_with_production_trace_is_rejected(self):
        self.invoice.items.append(Doc(item_code="VX-0017", warehouse=BAR,
                                      **{module.KEY_FIELD: "FORGED"}))
        self._refused()
        self.replay.assert_not_called()

    def test_failed_trusted_source_never_starts_replay(self):
        self.trusted.side_effect = module.ProductionError(module.UNVERIFIED)
        self._refused()
        self.lock.assert_not_called()
        self.replay.assert_not_called()

    def test_every_menu_line_requires_matching_source_projection(self):
        self.pairs = []
        self._refused()
        self.snapshot.assert_not_called()
        self.replay.assert_not_called()

    def test_missing_or_incorrect_completed_production_is_rejected(self):
        original = deepcopy(self.invoice.items[0])
        for field, value in ((module.WORK_ORDER_FIELD, None), (module.WORK_ORDER_FIELD, "OTHER-WO"),
                             (module.KEY_FIELD, None), (module.KEY_FIELD, "FORGED")):
            with self.subTest(field=field, value=value):
                self.invoice.items[0] = deepcopy(original)
                setattr(self.invoice.items[0], field, value)
                self.pairs[0] = (self.invoice.items[0], self.pairs[0][1])
                self._refused()
        self.invoice.items[0] = original
        self.pairs[0] = (original, self.pairs[0][1])
        self.work_orders.clear()
        self._refused()
        self.replay.assert_not_called()

    def test_incomplete_work_order_or_invalid_manufacture_aborts_before_replay(self):
        self.completed.side_effect = module.ProductionError(module.CHANGED)
        self._refused()
        self.completed.side_effect = None
        self.manufacture.side_effect = module.ProductionError(module.CHANGED)
        self._refused()
        self.replay.assert_not_called()

    def test_current_sale_and_manufacture_must_share_exact_posting_timestamp(self):
        self.state["sles"][1].posting_datetime += timedelta(seconds=1)
        self._refused()
        self.replay.assert_not_called()

    def test_current_sale_requires_exact_one_native_issue_per_row(self):
        self.state["sles"][1].voucher_detail_no = "WRONG-SII"
        self._refused()
        self.replay.assert_not_called()

    def test_native_commit_is_prohibited_and_framework_transaction_nesting_survives(self):
        original = self.db.commit
        self.replay.side_effect = lambda *args, **kwargs: self.db.commit()
        self._refused()
        self.assertIs(self.db.commit, original)
        self.assertEqual(self.db._disable_transaction_control, 2)
        original.assert_not_called()

    def test_auto_commit_on_many_writes_is_rejected_before_native_replay(self):
        self.db.auto_commit_on_many_writes = True
        self._refused()
        self.replay.assert_not_called()

    def test_native_exception_is_sanitized_and_transaction_control_restored(self):
        original = self.db.commit
        self.replay.side_effect = RuntimeError("private database details")
        with self.assertRaises(module.ProductionError) as raised:
            self._finalize()
        self.assertNotIn("private database", str(raised.exception))
        self.assertIs(self.db.commit, original)
        self.assertIsNone(module._WRITING.get())
        self.db.commit.assert_not_called()
        self.db.rollback.assert_not_called()

    def test_qty_balance_can_be_nonzero_when_later_valid_stock_exists(self):
        later = WHEN + timedelta(hours=1)
        self.state["sles"].append(sle(
            "SLE-LATER", quantity=3, balance=3, value=30000, difference=30000,
            voucher_no="SE-LATER", detail="SEI-LATER", when=later, sequence=3,
        ))
        self.state["sles"][-1].recalculate_rate = 0
        self.state["vouchers"][("Stock Entry", "SE-LATER")] = {
            "doctype": "Stock Entry", "name": "SE-LATER", "docstatus": 1,
            "company": module.COMPANY, "purpose": "Material Receipt", "items": [],
        }
        self._bin().actual_qty = self._bin().projected_qty = 3
        self._bin().stock_value = 40000
        self.assertEqual(self._finalize(), [PAN])
        self.assertEqual((self._bin().actual_qty, self._bin().stock_value), (3, 30000))

    def test_prior_baseline_is_included_without_replaying_earlier_history(self):
        previous = sle("SLE-BASELINE", when=WHEN - timedelta(days=1), sequence=0)
        self.state["previous"] = [previous]
        for row in self.state["sles"]:
            row.qty_after_transaction += 1
            row.stock_value += 10000
        self._bin().actual_qty = self._bin().projected_qty = 1
        self._bin().stock_value = 20000
        self.assertEqual(self._finalize(), [PAN])
        self.assertEqual(self._bin().stock_value, 10000)
        self.assertEqual(self.state["previous"], [previous])
        self.assertEqual(self.replay.call_args.args[0]["posting_date"], WHEN.date())

    def test_final_bin_must_match_native_latest_sle_even_after_allowed_value_refresh(self):
        def incorrect_bin(args, **kwargs):
            self._native_replay(args, **kwargs)
            self._bin().stock_value = 1

        self.replay.side_effect = incorrect_bin
        self._refused()
        self.replay.assert_called_once()

    def test_raw_and_unselected_menu_bins_are_not_permitted_to_change(self):
        baseline = deepcopy(self.state)
        for code, field in ((module.RAW, "stock_value"), (module.RAW, "actual_qty"),
                            (DRY, "stock_value"), (module.PORTION, "planned_qty"),
                            (PAN, "projected_qty"), (PAN, "reserved_qty_for_production")):
            with self.subTest(code=code, field=field):
                self.state = deepcopy(baseline)

                def change_unrelated(args, **kwargs):
                    self._native_replay(args, **kwargs)
                    row = self._bin(code)
                    row[field] += 1

                self.replay.side_effect = change_unrelated
                self._refused()

    def test_accounting_and_commercial_values_are_immutable_during_replay(self):
        baseline = deepcopy(self.state)
        mutations = {
            "gl_amount": lambda state: state["gl"][0].update(debit=53999),
            "gl_metadata": lambda state: state["gl"][0].update(modified_by="different@example.com"),
            "sales_price": lambda state: state["vouchers"][("Sales Invoice", "SI-1")]["items"][0].update(rate=1),
            "invoice_total": lambda state: state["vouchers"][("Sales Invoice", "SI-1")].update(grand_total=1),
            "tender": lambda state: state["vouchers"][("Sales Invoice", "SI-1")]["payments"][0].update(amount=1),
            "manufacture_amount": lambda state: state["vouchers"][("Stock Entry", "SE-1")]["items"][0].update(amount=1),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                self.state = deepcopy(baseline)

                def change_business(args, **kwargs):
                    self._native_replay(args, **kwargs)
                    mutate(self.state)

                self.replay.side_effect = change_business
                self._refused()

    def test_stock_value_changes_require_review_even_when_ledger_remains_balanced(self):
        def revalue(args, **kwargs):
            self._native_replay(args, **kwargs)
            first, last = self.state["sles"]
            first.stock_value = first.stock_value_difference = 9000
            last.stock_value_difference = -9000

        self.replay.side_effect = revalue
        self._refused()

    def test_only_kitchen_menu_item_derived_cost_rate_is_exempt(self):
        drink = Doc(doctype="Sales Invoice Item", name="SII-BAR", item_code="VX-0017",
                    warehouse=BAR, qty=1, rate=24000, incoming_rate=5000)
        self.invoice.items.append(drink)
        self.state["vouchers"][("Sales Invoice", "SI-1")] = self.invoice.as_dict()

        def change_bar_rate(args, **kwargs):
            self._native_replay(args, **kwargs)
            self.state["vouchers"][("Sales Invoice", "SI-1")]["items"][1]["incoming_rate"] = 0

        self.replay.side_effect = change_bar_rate
        self._refused()
        self.assertEqual(self.replay.call_args.args[0]["warehouse"], module.WAREHOUSE)
        self.assertEqual(self.replay.call_args.args[0]["item_code"], PAN)

    def test_native_metadata_and_dish_cost_refresh_are_accepted(self):
        self.state["vouchers"][("Sales Invoice", "SI-1")]["items"][0]["modified_by"] = "other@example.com"
        self._bin().modified_by = "other@example.com"
        self.assertEqual(self._finalize(), [PAN])
        item = self.state["vouchers"][("Sales Invoice", "SI-1")]["items"][0]
        self.assertEqual((item["incoming_rate"], item["rate"]), (10000, 30000))
        self.assertEqual(item["modified_by"], USER)

    def test_balance_helper_returns_latest_sle_without_mutating_evidence(self):
        before = deepcopy(self.state)
        result = module._valuation_balances(self.state, (PAN,))
        self.assertEqual(result, {PAN: self.state["sles"][-1]})
        self.assertEqual(self.state, before)

    def test_negative_stock_setting_cannot_permit_negative_ledger_prefix(self):
        self.db.get_single_value.return_value = 1
        self.state["sles"][0].actual_qty = -1
        self.state["sles"][0].qty_after_transaction = -1
        self._refused()
        self.replay.assert_not_called()

    def test_invalid_quantity_value_or_valuation_fails_before_replay(self):
        baseline = deepcopy(self.state)
        cases = (
            (0, "actual_qty", "NaN"), (0, "actual_qty", "Infinity"),
            (0, "qty_after_transaction", -1), (0, "qty_after_transaction", 2),
            (0, "stock_value_difference", 9999), (0, "stock_value_difference", "NaN"),
            (0, "stock_value", -1), (0, "valuation_rate", "NaN"),
            (0, "valuation_rate", -1), (1, "qty_after_transaction", -1),
        )
        for index, field, value in cases:
            with self.subTest(index=index, field=field, value=value):
                self.state = deepcopy(baseline)
                self.state["sles"][index][field] = value
                self._refused()
        self.replay.assert_not_called()

    def test_zero_quantity_cannot_retain_nonzero_sle_value(self):
        self.state["sles"][1].stock_value = 1
        self.state["sles"][1].stock_value_difference = -9999
        self._refused()
        self.replay.assert_not_called()

    def test_bad_bin_quantity_is_not_silently_repaired_without_projected_qty(self):
        self._bin().actual_qty = 1
        self._bin().projected_qty = 1
        self._refused()
        self.replay.assert_not_called()

    def test_missing_duplicate_or_misrouted_finished_bin_is_rejected(self):
        baseline = deepcopy(self.state)
        for label in ("missing", "duplicate", "wrong_warehouse"):
            with self.subTest(label=label):
                self.state = deepcopy(baseline)
                if label == "missing":
                    self.state["bins"] = [row for row in self.state["bins"] if row.item_code != PAN]
                elif label == "duplicate":
                    self.state["bins"].append(deepcopy(self._bin()))
                else:
                    self._bin().warehouse = BAR
                self._refused()
        self.replay.assert_not_called()

    def test_foreign_company_warehouse_or_item_cannot_enter_replay_bucket(self):
        baseline = deepcopy(self.state)
        for field, value in (("company", "4S Industries Limited"), ("warehouse", BAR),
                             ("item_code", module.RAW), ("is_cancelled", 1)):
            with self.subTest(field=field, value=value):
                self.state = deepcopy(baseline)
                self.state["sles"][0][field] = value
                self._refused()
        self.replay.assert_not_called()

    def test_dependencies_serial_batch_and_adjustment_entries_are_rejected(self):
        baseline = deepcopy(self.state)
        for field, value in (("dependant_sle_voucher_detail_no", "BAR-TRANSFER-ROW"),
                             ("serial_no", "SERIAL-1"), ("batch_no", "BATCH-1"),
                             ("serial_and_batch_bundle", "BUNDLE-1"),
                             ("has_serial_no", 1), ("has_batch_no", 1), ("is_adjustment_entry", 1)):
            with self.subTest(field=field):
                self.state = deepcopy(baseline)
                self.state["sles"][0][field] = value
                self._refused()
        self.replay.assert_not_called()

    def test_unsupported_stock_voucher_types_are_rejected(self):
        baseline = deepcopy(self.state)
        for doctype in ("Stock Reconciliation", "Delivery Note", "Purchase Receipt"):
            with self.subTest(doctype=doctype):
                self.state = deepcopy(baseline)
                self.state["sles"][0].voucher_type = doctype
                self._refused()
        self.replay.assert_not_called()

    def test_identical_posting_and_creation_ties_are_ambiguous_even_with_different_names(self):
        self.state["sles"][1].creation = self.state["sles"][0].creation
        self.assertNotEqual(self.state["sles"][0].name, self.state["sles"][1].name)
        self._refused()
        self.replay.assert_not_called()

    def test_current_manufacture_must_precede_invoice_creation_within_timestamp(self):
        self.state["sles"][0].creation = CREATED + timedelta(microseconds=3)
        self._refused()
        self.replay.assert_not_called()

    def test_invalid_historical_baseline_fails_closed_before_replay(self):
        baseline = sle("SLE-BASELINE", when=WHEN - timedelta(days=1), sequence=0,
                       quantity=0, balance=0, value=0, difference=0)
        cases = (("stock_value", 10000), ("qty_after_transaction", -1),
                 ("valuation_rate", "NaN"), ("is_cancelled", 1),
                 ("company", "4S Industries Limited"), ("warehouse", BAR))
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.state["previous"] = [deepcopy(baseline)]
                self.state["previous"][0][field] = value
                self._refused()
        self.replay.assert_not_called()

    def test_extra_current_invoice_stock_entry_cannot_hide_behind_zero_net_quantity(self):
        self.state["sles"].append(sle(
            "SLE-EXTRA", quantity=0, balance=0, value=0, difference=0,
            voucher_type="Sales Invoice", voucher_no=self.invoice.name,
            detail="SII-UNEXPECTED", sequence=3,
        ))
        self._refused()
        self.replay.assert_not_called()

    def test_wrong_current_manufacture_voucher_or_issue_quantity_is_rejected(self):
        baseline = deepcopy(self.state)
        self.state["sles"][0].voucher_no = "UNRELATED-MANUFACTURE"
        self._refused()
        self.state = deepcopy(baseline)
        self.state["sles"][0].actual_qty = self.state["sles"][0].qty_after_transaction = 2
        self.state["sles"][1].actual_qty = -2
        self._refused()
        self.replay.assert_not_called()

    def test_disappearing_or_new_ledger_entries_during_replay_abort(self):
        baseline = deepcopy(self.state)
        for label in ("disappearing", "new"):
            with self.subTest(label=label):
                self.state = deepcopy(baseline)

                def change_rows(args, **kwargs):
                    self._native_replay(args, **kwargs)
                    if label == "disappearing":
                        self.state["sles"] = []
                    else:
                        self.state["sles"].append(sle(
                            "SLE-NEW", quantity=0, balance=0, value=0, difference=0,
                            voucher_type="Sales Invoice", voucher_no="SI-NEW",
                            detail="SII-NEW", sequence=3,
                        ))

                self.replay.side_effect = change_rows
                self._refused()

    def test_snapshot_uses_current_locking_reads_and_exact_replay_boundary(self):
        actual = self._read_snapshot()
        self.assertEqual(actual, self.state)
        self.assertTrue(all("FOR UPDATE" in call.args[0] for call in self.db.sql.call_args_list))
        queries = [call.args[0] for call in self.db.sql.call_args_list]
        self.assertTrue(any("posting_datetime>=" in query and "LIMIT 2001" in query for query in queries))
        self.assertTrue(any("posting_datetime<" in query and "LIMIT 2" in query for query in queries))
        self.frappe.get_doc.assert_any_call("Sales Invoice", "SI-1", for_update=True)
        self.frappe.get_doc.assert_any_call("Stock Entry", "SE-1", for_update=True)
        self.db.commit.assert_not_called()

    def test_snapshot_rejects_empty_or_oversized_buckets(self):
        first = deepcopy(self.state["sles"][0])
        for values in ([], [first] * 2001):
            with self.subTest(size=len(values)):
                self.state["sles"] = values
                with self.assertRaises(module.ProductionError):
                    self._read_snapshot()
        self.replay.assert_not_called()

    def test_snapshot_rejects_ambiguous_latest_baseline_creation_tie(self):
        previous = sle("PREVIOUS-A", when=WHEN - timedelta(days=1), sequence=0)
        duplicate = deepcopy(previous)
        duplicate.name = "PREVIOUS-B"
        self.state["previous"] = [previous, duplicate]
        with self.assertRaises(module.ProductionError):
            self._read_snapshot()
        self.replay.assert_not_called()

    def test_snapshot_preserves_latest_baseline_but_ignores_older_unambiguous_row(self):
        latest = sle("PREVIOUS-LATEST", when=WHEN - timedelta(days=1), sequence=0)
        older = sle("PREVIOUS-OLDER", when=WHEN - timedelta(days=2), sequence=0)
        self.state["previous"] = [latest, older]
        snapshot = self._read_snapshot()
        self.assertEqual(snapshot["previous"], [latest])

    def test_snapshot_rejects_unsubmitted_or_foreign_stock_vouchers(self):
        original = deepcopy(self.state["vouchers"])
        for field, value in (("docstatus", 0), ("docstatus", 2), ("company", "4S Industries Limited")):
            with self.subTest(field=field, value=value):
                self.state["vouchers"] = deepcopy(original)
                self.state["vouchers"][("Stock Entry", "SE-1")][field] = value
                with self.assertRaises(module.ProductionError):
                    self._read_snapshot()
        self.replay.assert_not_called()

    def test_queue_numeric_formatting_does_not_count_as_stock_revaluation(self):
        def reformat(args, **kwargs):
            self._native_replay(args, **kwargs)
            self.state["sles"][0].stock_queue = "[[1,10000]]"

        self.replay.side_effect = reformat
        self.assertEqual(self._finalize(), [PAN])

    def _add_second_current_dish(self, code):
        line_uuid = "00000000-0000-4000-8000-000000000002"
        item = deepcopy(self.invoice.items[0])
        item.name, item.idx, item.item_code = "SII-2", 2, code
        setattr(item, module.KEY_FIELD, module.production_key(self.sale.sale_uuid, line_uuid))
        setattr(item, module.WORK_ORDER_FIELD, "WO-2")
        self.invoice.items.append(item)
        self.pairs.append((item, Doc(line_id="15807", desktop_line_uuid=line_uuid)))
        self.work_orders[line_uuid] = Doc(name="WO-2", status="Completed")
        entry = deepcopy(self.entry)
        entry.name, entry.work_order, entry.bom_no = "SE-2", "WO-2", module.MENU[code]
        entry.items[0].name = "SEI-RAW-2"
        entry.items[1].name, entry.items[1].item_code = "SEI-FG-2", code
        self.entries["WO-2"] = entry
        self.state["vouchers"][("Stock Entry", "SE-2")] = entry.as_dict()
        self.state["vouchers"][("Sales Invoice", "SI-1")] = self.invoice.as_dict()
        self.state["sles"][1].creation = CREATED + timedelta(microseconds=3)
        self.state["sles"].insert(1, sle(
            "SLE-MAKE-2", code=code, voucher_no="SE-2", detail="SEI-FG-2", sequence=2,
        ))
        self.state["sles"].append(sle(
            "SLE-SALE-2", code=code, quantity=-1, balance=0, value=0, difference=-10000,
            voucher_type="Sales Invoice", voucher_no="SI-1", detail="SII-2", sequence=4,
        ))
        if code == PAN:
            self.state["sles"][1].qty_after_transaction = 2
            self.state["sles"][1].stock_value = 20000
            self.state["sles"][1].stock_queue = "[[2, 10000]]"
            self.state["sles"][2].qty_after_transaction = 1
            self.state["sles"][2].stock_value = 10000
            self.state["sles"][2].stock_queue = "[[1, 10000]]"
            self._bin().stock_value = 20000
        else:
            self._bin(code).stock_value = self._bin(code).valuation_rate = 10000

    def test_repeated_same_style_lines_replay_each_item_only_once(self):
        self._add_second_current_dish(PAN)
        self.assertEqual(self._finalize(), [PAN])
        self.replay.assert_called_once()
        self.assertEqual(self.completed.call_count, 2)
        self.assertEqual(self.manufacture.call_count, 2)
        self.assertEqual(self._bin().stock_value, 0)

    def test_two_menu_styles_replay_once_each_in_stable_item_order(self):
        self._add_second_current_dish(DRY)
        self.assertEqual(self._finalize(), [DRY, PAN])
        self.assertEqual([call.args[0]["item_code"] for call in self.replay.call_args_list], [DRY, PAN])
        self.assertEqual(self._bin(PAN).stock_value, 0)
        self.assertEqual(self._bin(DRY).stock_value, 0)
        self.assertEqual(self._bin(module.RAW).stock_value, 40000)

    def test_later_replay_failure_never_commits_an_earlier_item(self):
        self._add_second_current_dish(DRY)
        original = self.db.commit

        def fail_second(args, **kwargs):
            if args["item_code"] == PAN:
                raise RuntimeError("replay interrupted")
            return self._native_replay(args, **kwargs)

        self.replay.side_effect = fail_second
        self._refused()
        self.assertEqual(self.replay.call_count, 2)
        self.assertIs(self.db.commit, original)
        original.assert_not_called()

    def test_zero_quantity_zero_value_placeholder_may_refresh_its_unused_rate(self):
        self.state["sles"][1].stock_queue = "[[0, 20000.0]]"

        def refresh_zero_placeholder(args, **kwargs):
            self._native_replay(args, **kwargs)
            self.state["sles"][1].stock_queue = "[[0.0, 10000]]"

        self.replay.side_effect = refresh_zero_placeholder
        self.assertEqual(self._finalize(), [PAN])
        self.assertEqual(self._bin().stock_value, 0)

    def test_nonzero_fifo_layer_quantity_or_rate_remains_immutable(self):
        baseline = deepcopy(self.state)
        cases = (
            (0, "[[1, 10000]]", "[[1, 9000]]"),
            (0, "[[1, 10000]]", "[[2, 10000]]"),
            (1, "[[-1, 10000], [1, 10000]]", "[[-1, 9000], [1, 10000]]"),
            (1, "[[-1, 10000], [1, 10000]]", "[[-2, 10000], [1, 10000]]"),
            (1, "[[-1, 10000], [1, 10000]]", "[[-1, 10000], [1, 9000]]"),
        )
        for index, before, after in cases:
            with self.subTest(index=index, before=before, after=after):
                self.state = deepcopy(baseline)
                self.state["sles"][index].stock_queue = before

                def change_real_layer(args, **kwargs):
                    self._native_replay(args, **kwargs)
                    self.state["sles"][index].stock_queue = after

                self.replay.side_effect = change_real_layer
                self._refused()

    def test_zero_placeholder_rate_is_not_exempt_while_stock_remains(self):
        self.state["sles"][0].stock_queue = "[[1, 10000], [0, 25000]]"

        def change_placeholder_with_stock(args, **kwargs):
            self._native_replay(args, **kwargs)
            self.state["sles"][0].stock_queue = "[[1, 10000], [0, 0]]"

        self.replay.side_effect = change_placeholder_with_stock
        self._refused()

    def test_zero_placeholder_queue_shape_and_layer_count_are_not_discarded(self):
        baseline = deepcopy(self.state)
        for after in ("[]", "[[0, 0], [0, 0]]"):
            with self.subTest(after=after):
                self.state = deepcopy(baseline)
                self.state["sles"][1].stock_queue = "[[0, 10000]]"

                def change_shape(args, **kwargs):
                    self._native_replay(args, **kwargs)
                    self.state["sles"][1].stock_queue = after

                self.replay.side_effect = change_shape
                self._refused()

    def test_unchanged_invalid_or_nonfinite_zero_placeholders_are_rejected(self):
        baseline = deepcopy(self.state)
        for queue in (
            "[[0, NaN]]", "[[0, Infinity]]", "[[0, -Infinity]]", "[[0, -1]]",
            "[[0, null]]", "[[0, true]]", '[[0, "not-a-number"]]',
            "[[NaN, 0]]", "[[0]]", "[[0, 0, 0]]", "[0, 0]", "{}",
        ):
            with self.subTest(queue=queue):
                self.state = deepcopy(baseline)
                self.state["sles"][1].stock_queue = queue
                self._refused()
