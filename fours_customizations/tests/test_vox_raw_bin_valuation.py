"""Write-free proof that raw cache settlement cannot become a stock adjustment."""

from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from types import ModuleType
from unittest import TestCase
from unittest.mock import Mock, patch

from fours_customizations import vox_work_orders as module
from fours_customizations.tests import test_vox_work_orders as legacy
from fours_customizations.tests import test_vox_invoice_valuation as valuation
from fours_customizations.tests import test_vox_kitchen_multi_ingredient as recipes


Row, Doc = valuation.Row, valuation.Doc
WHEN = datetime(2026, 9, 28, 23, 9, 40)
BIRD, IRISH, GOAT = recipes.BIRD, recipes.IRISH, module.RAW
CODES = tuple(sorted((BIRD, IRISH, GOAT)))


def fixture():
    balances = {BIRD: Decimal(1), IRISH: Decimal(1), GOAT: Decimal("0.8")}
    rates = {BIRD: Decimal(40000), IRISH: Decimal(2000), GOAT: Decimal(25000)}
    values = {code: balances[code] * rates[code] for code in CODES}
    previous = []
    for index, code in enumerate(CODES):
        row = valuation.sle("BASE-" + code, code=code, quantity=float(balances[code]), balance=float(balances[code]),
                            value=float(values[code]), difference=float(values[code]),
                            when=WHEN - timedelta(days=1), sequence=index)
        row.stock_uom, row.valuation_rate = module.RAW_UOMS[code], float(rates[code])
        previous.append(row)
    made, entries, sles, voucher_sles, work_orders, vouchers, gl = [], {}, [], [], {}, {}, []
    for index, code in enumerate(recipes.NEW):
        value = recipes.plan(code, line=f"{index + 1}8d310b5-8c03-4ce7-9503-8e54e975d81f")
        wo = recipes.work_order(value)
        wo.name = f"WO-{index}"
        entry = Doc(**vars(recipes.manufacture(value)))
        entry.name, entry.work_order, entry.docstatus, entry.purpose = f"SE-{index}", wo.name, 1, "Manufacture"
        fg = entry.items[-1]
        fg.name = f"SEI-{index}-FG"
        cost = Decimal(0)
        for number, part in enumerate(entry.items[:-1]):
            part.name = f"SEI-{index}-{number}"
            part.basic_amount = float(Decimal(str(part.qty)) * rates[part.item_code])
            cost += Decimal(str(part.basic_amount))
            balances[part.item_code] -= Decimal(str(part.qty))
            values[part.item_code] -= Decimal(str(part.basic_amount))
            row = valuation.sle(f"SLE-{index}-{number}", code=part.item_code,
                quantity=-part.qty, balance=float(balances[part.item_code]), value=float(values[part.item_code]),
                difference=-part.basic_amount, voucher_no=entry.name, detail=part.name, sequence=index * 10 + number)
            row.stock_uom = part.stock_uom
            row.valuation_rate = float(rates[part.item_code])
            row.dependant_sle_voucher_detail_no = fg.name
            sles.append(row)
            voucher_sles.append(row)
        fg.basic_amount = float(cost)
        voucher_sles.append(valuation.sle(f"SLE-{index}-FG", code=code, value=float(cost),
            difference=float(cost), voucher_no=entry.name, detail=fg.name, sequence=index * 10 + 8))
        entries[wo.name] = entry
        made.append((value, wo))
        work_orders[wo.name] = wo.as_dict()
        vouchers[("Stock Entry", entry.name)] = entry.as_dict()
        for number, (debit, credit) in enumerate(((float(cost), 0), (0, float(cost)))):
            gl.append(Row(name=f"GL-{index}-{number}", voucher_type="Stock Entry", voucher_no=entry.name,
                          company=module.COMPANY, is_cancelled=0, account="Kitchen stock - VXN",
                          debit=debit, credit=credit))
    bins = []
    for code in CODES:
        stale = {BIRD: 30000, IRISH: 1800, GOAT: 10000}[code]
        row = valuation.bin_row(code, quantity=float(balances[code]), value=stale, rate=float(rates[code]))
        row.stock_uom = module.RAW_UOMS[code]
        bins.append(row)
    bins.extend(valuation.bin_row(code, quantity=1, value=10000, rate=10000) for code in recipes.NEW)
    return made, entries, {"sles": sles, "previous": previous, "voucher_sles": voucher_sles,
                           "vouchers": vouchers, "work_orders": work_orders, "gl": gl, "bins": bins}


class TestVoxRawBinValuation(TestCase):
    def setUp(self):
        legacy.TestVoxWorkOrders.setUp(self)
        self.made, self.entries, self.state = fixture()
        self.snapshot_function = module._raw_valuation_snapshot
        self.precisions_function = module._raw_valuation_precisions
        self.snapshot = self.enterContext(patch.object(module, "_raw_valuation_snapshot", side_effect=lambda *a: deepcopy(self.state)))
        self.precisions = self.enterContext(patch.object(module, "_raw_valuation_precisions", return_value=(2, 2, 2)))
        self.manufacture = self.enterContext(patch.object(module, "_manufacture_for", side_effect=lambda wo, plan: self.entries[wo.name]))
        self.native = ModuleType("erpnext.stock.stock_balance")
        self.native.update_bin_qty = self.update = Mock(side_effect=self.native_update)
        self.enterContext(patch.dict("sys.modules", {self.native.__name__: self.native}))

    def bin(self, code):
        return next(row for row in self.state["bins"] if row.item_code == code)

    def native_update(self, code, warehouse, values):
        self.assertEqual(warehouse, module.WAREHOUSE)
        self.assertEqual(set(values), {"stock_value", "valuation_rate"})
        self.bin(code).update(values)
        self.bin(code).modified = "native cache refresh"

    def settle(self, *, made=None, context_change=None):
        with module._native_writes() as context:
            for _plan, wo in self.made:
                context.objects[id(wo)] = wo
                entry = self.entries[wo.name]
                context.objects[id(entry)] = entry
            if context_change:
                context_change(context)
            return module._settle_raw_bin_values(self.made if made is None else made, context)

    def test_exact_native_mixed_recipe_fixes_only_two_stale_raw_cache_values(self):
        before = deepcopy(self.state)
        self.assertEqual(self.settle(), [BIRD, IRISH])
        self.assertEqual(self.bin(BIRD).actual_qty, 0.5)
        self.assertEqual(self.bin(BIRD).stock_value, 20000)
        self.assertEqual(self.bin(IRISH).actual_qty, 0.8)
        self.assertEqual(self.bin(IRISH).stock_value, 1600)
        self.assertEqual(self.bin(GOAT).stock_value, 10000)
        self.assertEqual(module._raw_cache_frozen(before, CODES), module._raw_cache_frozen(self.state, CODES))
        self.assertEqual(self.update.call_count, 2)
        self.db.commit.assert_not_called()

    def test_already_matching_values_are_a_verified_noop(self):
        self.settle()
        self.update.reset_mock()
        before = deepcopy(self.state)
        self.assertEqual(self.settle(), [])
        self.update.assert_not_called()
        self.assertEqual(self.state, before)

    def test_production_context_and_both_owned_native_objects_are_required(self):
        with self.assertRaises(module.ProductionError):
            module._settle_raw_bin_values(self.made, None)
        with self.assertRaises(module.ProductionError):
            self.settle(context_change=lambda context: context.objects.clear())
        self.update.assert_not_called()

    def test_duplicate_work_order_or_empty_set_cannot_authorize_raw_refresh(self):
        for made in ([], [*self.made, self.made[0]]):
            with self.subTest(count=len(made)), self.assertRaises(module.ProductionError):
                self.settle(made=made)
        self.update.assert_not_called()

    def test_changed_work_order_plan_or_native_timestamp_aborts_before_update(self):
        value, wo = self.made[0]
        value["posting_datetime"] = "2026-09-28 23:09:41"
        with self.assertRaises(module.ProductionError):
            self.settle()
        self.update.assert_not_called()

    def test_missing_bin_cannot_trigger_native_get_bin_creation(self):
        self.state["bins"] = [row for row in self.state["bins"] if row.item_code != BIRD]
        with self.assertRaises(module.ProductionError):
            self.settle()
        self.update.assert_not_called()

    def test_duplicate_bin_is_rejected_even_if_values_match(self):
        self.state["bins"].append(deepcopy(self.bin(BIRD)))
        with self.assertRaises(module.ProductionError):
            self.settle()
        self.update.assert_not_called()

    def test_stale_quantity_is_never_silently_fixed_using_a_stock_value_refresh(self):
        self.bin(BIRD).actual_qty = 0.75
        with self.assertRaises(module.ProductionError):
            self.settle()
        self.update.assert_not_called()

    def test_wrong_bin_warehouse_unit_or_company_aborts(self):
        for field, invalid in (("warehouse", "Bar - VXN"), ("stock_uom", "Nos"),
                               ("company", "Vox Lounge")):
            _made, _entries, self.state = fixture()
            self.bin(BIRD)[field] = invalid
            with self.subTest(field=field), self.assertRaises(module.ProductionError):
                self.settle()
        self.update.assert_not_called()

    def test_every_raw_ledger_row_rejects_foreign_unit_warehouse_company_or_cancelled_state(self):
        for field, invalid in (("warehouse", "Bar - VXN"), ("stock_uom", "Kg"),
                               ("company", "Vox Lounge"), ("is_cancelled", 1), ("item_code", "FOREIGN")):
            _made, _entries, self.state = fixture()
            self.state["sles"][0][field] = invalid
            with self.subTest(field=field), self.assertRaises(module.ProductionError):
                self.settle()
        self.update.assert_not_called()

    def test_serial_batch_and_adjustment_ledgers_are_outside_the_recipe_contract(self):
        for field in ("serial_no", "batch_no", "serial_and_batch_bundle", "has_serial_no", "has_batch_no", "is_adjustment_entry"):
            _made, _entries, self.state = fixture()
            self.state["sles"][0][field] = 1
            with self.subTest(field=field), self.assertRaises(module.ProductionError):
                self.settle()
        self.update.assert_not_called()

    def test_nonfinite_negative_or_inconsistent_raw_values_fail_before_update(self):
        for field, invalid in (("actual_qty", "NaN"), ("qty_after_transaction", -1),
                               ("stock_value", -1), ("valuation_rate", "Infinity"),
                               ("stock_value_difference", -9999), ("stock_value", 30001)):
            _made, _entries, self.state = fixture()
            self.state["sles"][0][field] = invalid
            with self.subTest(field=field), self.assertRaises((module.ProductionError, ArithmeticError)):
                self.settle()
        self.update.assert_not_called()

    def test_negative_cached_value_is_not_laundered_into_a_positive_value(self):
        self.bin(BIRD).stock_value = -1
        with self.assertRaises(module.ProductionError):
            self.settle()
        self.update.assert_not_called()

    def test_positive_but_inconsistent_ledger_rate_is_not_copied_to_bin(self):
        for rate in (1, 80000):
            _made, _entries, self.state = fixture()
            self.state["sles"][1].valuation_rate = rate
            with self.subTest(rate=rate), self.assertRaises(module.ProductionError):
                self.settle()
        self.update.assert_not_called()

    def test_native_currency_rounding_and_zero_quantity_fallback_rate_remain_valid(self):
        module._raw_valuation_rate_matches(Row(qty_after_transaction=0.3, stock_value=1, valuation_rate=3.333333333), (2, 2, 2))
        module._raw_valuation_rate_matches(Row(qty_after_transaction=0, stock_value=0, valuation_rate=40000), (2, 2, 2))
        with self.assertRaises(module.ProductionError):
            module._raw_valuation_rate_matches(Row(qty_after_transaction=0.5, stock_value=20000, valuation_rate=1), (2, 2, 2))

    def test_precision_lookup_matches_native_stock_value_rate_and_float_settings(self):
        from frappe.model import meta

        fields = {name: Row(fieldname=name) for name in ("stock_value", "valuation_rate")}
        metadata = Doc(get_field=lambda name: fields[name])
        self.frappe.get_meta = Mock(return_value=metadata)
        self.db.get_default = Mock(return_value="3")
        with patch.object(meta, "get_field_precision", side_effect=[2, 4]) as precision:
            self.assertEqual(self.precisions_function(), (2, 4, 3))
        self.assertEqual([call.args[0].fieldname for call in precision.call_args_list], ["stock_value", "valuation_rate"])
        self.db.get_default.assert_called_once_with("float_precision")

    def test_invalid_precision_cannot_expand_the_allowed_rounding_envelope(self):
        from frappe.model import meta

        self.frappe.get_meta = Mock(return_value=Doc(get_field=lambda name: Row(fieldname=name)))
        self.db.get_default = Mock(return_value="2")
        for invalid in (None, -1, 10, True, "2"):
            with patch.object(meta, "get_field_precision", side_effect=[invalid, 2]), \
                    self.subTest(invalid=invalid), self.assertRaises(module.ProductionError):
                self.precisions_function()

    def test_wrong_raw_cost_is_rejected_even_if_quantity_chain_is_correct(self):
        self.state["sles"][0].stock_value_difference = -9000
        self.state["sles"][0].stock_value = 31000
        with self.assertRaises(module.ProductionError):
            self.settle()
        self.update.assert_not_called()

    def test_dependency_must_point_to_same_completed_native_manufacture_finished_row(self):
        for mutate in (lambda state: setattr(state["sles"][0], "dependant_sle_voucher_detail_no", "FOREIGN-FG"),
                       lambda state: setattr(state["sles"][0], "dependant_sle_voucher_detail_no", None),
                       lambda state: state["work_orders"]["WO-0"].update(status="Not Started"),
                       lambda state: state["vouchers"][("Stock Entry", "SE-0")].update(work_order="FOREIGN-WO")):
            _made, _entries, self.state = fixture()
            mutate(self.state)
            with self.assertRaises(module.ProductionError):
                self.settle()
        self.update.assert_not_called()

    def test_incorrect_finished_target_quantity_or_value_cannot_be_hidden_by_raw_cache_settlement(self):
        for field, invalid in (("actual_qty", 2), ("stock_value_difference", 9999), ("warehouse", "Bar - VXN")):
            _made, _entries, self.state = fixture()
            row = next(row for row in self.state["voucher_sles"] if row.item_code == recipes.CHICKEN)
            row[field] = invalid
            with self.subTest(field=field), self.assertRaises(module.ProductionError):
                self.settle()
        self.update.assert_not_called()

    def test_new_manufacture_sles_cannot_disappear_or_gain_an_extra_adjustment(self):
        self.state["voucher_sles"].append(deepcopy(self.state["voucher_sles"][0]))
        with self.assertRaises(module.ProductionError):
            self.settle()
        self.update.assert_not_called()

    def test_unbalanced_or_foreign_native_manufacture_gl_is_rejected(self):
        for field, invalid in (("debit", 1), ("company", "Vox Lounge"), ("is_cancelled", 1)):
            _made, _entries, self.state = fixture()
            self.state["gl"][0][field] = invalid
            with self.subTest(field=field), self.assertRaises(module.ProductionError):
                self.settle()
        self.update.assert_not_called()

    def test_balanced_same_account_manufacture_can_legitimately_have_no_gl_rows(self):
        self.state["gl"] = []
        self.assertEqual(self.settle(), [BIRD, IRISH])

    def test_ambiguous_or_unordered_raw_timeline_is_rejected(self):
        duplicate = deepcopy(self.state["sles"][0])
        duplicate.name = "ANOTHER-SAME-CREATION"
        self.state["sles"].append(duplicate)
        with self.assertRaises(module.ProductionError):
            self.settle()
        _made, _entries, self.state = fixture()
        self.state["sles"].reverse()
        with self.assertRaises(module.ProductionError):
            self.settle()
        self.update.assert_not_called()

    def add_future_receipt(self):
        when = WHEN + timedelta(hours=1)
        row = valuation.sle("SLE-LATER-BIRD", code=BIRD, quantity=0.5, balance=1, value=40000,
                            difference=20000, voucher_no="SE-LATER", detail="SEI-LATER", when=when, sequence=100)
        row.stock_uom, row.valuation_rate = "Bird", 40000
        self.state["sles"].append(row)
        self.state["voucher_sles"].append(row)
        self.state["vouchers"][("Stock Entry", "SE-LATER")] = {
            "doctype": "Stock Entry", "name": "SE-LATER", "company": module.COMPANY, "docstatus": 1,
            "purpose": "Material Receipt", "items": [{"name": "SEI-LATER", "item_code": BIRD,
                "t_warehouse": module.WAREHOUSE, "qty": 0.5, "basic_amount": 20000}],
        }
        self.bin(BIRD).actual_qty = self.bin(BIRD).projected_qty = 1

    def test_latest_legitimate_future_receipt_not_current_manufacture_is_authoritative(self):
        self.add_future_receipt()
        self.settle()
        self.assertEqual(self.bin(BIRD).actual_qty, 1)
        self.assertEqual(self.bin(BIRD).stock_value, 40000)

    def test_inconsistent_future_value_chain_aborts_instead_of_attempting_repost(self):
        self.add_future_receipt()
        self.state["sles"][-1].stock_value = 50000
        with self.assertRaises(module.ProductionError):
            self.settle()
        self.update.assert_not_called()

    def test_quantity_reset_reconciliation_requires_review_instead_of_cache_reconstruction(self):
        self.add_future_receipt()
        self.state["sles"][-1].voucher_type = "Stock Reconciliation"
        self.state["sles"][-1].actual_qty = 0
        self.state["vouchers"][("Stock Reconciliation", "SE-LATER")] = self.state["vouchers"].pop(("Stock Entry", "SE-LATER"))
        with self.assertRaises(module.ProductionError):
            self.settle()
        self.update.assert_not_called()

    def test_physical_quantities_reservations_projected_balance_and_other_bins_are_frozen(self):
        for code, field in ((BIRD, "actual_qty"), (BIRD, "projected_qty"), (BIRD, "reserved_stock"),
                            (BIRD, "reserved_qty_for_production"), (BIRD, "ordered_qty"),
                            (BIRD, "indented_qty"), (BIRD, "planned_qty"),
                            (recipes.CHICKEN, "stock_value"), (recipes.CHICKEN, "actual_qty")):
            _made, _entries, self.state = fixture()

            def bad_update(raw, warehouse, values):
                self.native_update(raw, warehouse, values)
                self.bin(code)[field] += 1

            self.update.side_effect = bad_update
            with self.subTest(code=code, field=field), self.assertRaises(module.ProductionError):
                self.settle()
        self.db.commit.assert_not_called()

    def test_any_ledger_gl_work_order_or_manufacture_change_during_cache_update_aborts(self):
        for mutate in (lambda state: state["sles"][0].update(modified="tampered"),
                       lambda state: state["voucher_sles"][-1].update(stock_value=1),
                       lambda state: state["gl"][0].update(account="Different Account"),
                       lambda state: state["work_orders"]["WO-0"].update(description="Changed"),
                       lambda state: state["vouchers"][("Stock Entry", "SE-0")].update(remarks="Changed")):
            _made, _entries, self.state = fixture()

            def bad_update(code, warehouse, values):
                self.native_update(code, warehouse, values)
                mutate(self.state)

            self.update.side_effect = bad_update
            with self.assertRaises(module.ProductionError):
                self.settle()
        self.db.commit.assert_not_called()

    def test_native_cache_updater_cannot_commit_or_silently_leave_values_wrong(self):
        self.update.side_effect = lambda *args: self.db.commit()
        with self.assertRaises(module.ProductionError):
            self.settle()
        self.update.side_effect = lambda *args: None
        with self.assertRaises(module.ProductionError):
            self.settle()
        self.db.commit.assert_not_called()

    def test_snapshot_caps_raw_and_full_voucher_ledgers_and_gl(self):
        for first in ([], [self.state["sles"][0]] * 2001):
            self.db.sql.side_effect = [first]
            with self.assertRaises(module.ProductionError):
                self.snapshot_function(CODES, WHEN)
        by_wo = {wo.name: wo for _value, wo in self.made}
        by_entry = {entry.name: entry for entry in self.entries.values()}
        self.frappe.get_doc.side_effect = lambda doctype, name, **kwargs: by_wo[name] if doctype == "Work Order" else by_entry[name]
        baseline = [self.state["sles"], *[[row] for row in self.state["previous"]]]
        self.db.sql.side_effect = [*baseline, self.state["voucher_sles"] * 300]
        with self.assertRaises(module.ProductionError):
            self.snapshot_function(CODES, WHEN)
        self.db.sql.side_effect = [*baseline, self.state["voucher_sles"], self.state["gl"] * 700]
        with self.assertRaises(module.ProductionError):
            self.snapshot_function(CODES, WHEN)
        self.update.assert_not_called()

    def test_native_snapshot_uses_locking_reads_and_includes_all_raw_and_finished_bins(self):
        self.db.sql.side_effect = [self.state["sles"], *[[row] for row in self.state["previous"]],
                                   self.state["voucher_sles"], self.state["gl"], self.state["bins"]]
        by_wo = {wo.name: wo for _value, wo in self.made}
        by_entry = {entry.name: entry for entry in self.entries.values()}
        self.frappe.get_doc.side_effect = lambda doctype, name, **kwargs: by_wo[name] if doctype == "Work Order" else by_entry[name]
        result = self.snapshot_function(CODES, WHEN)
        self.assertEqual(result, self.state)
        self.assertTrue(all("FOR UPDATE" in call.args[0] for call in self.db.sql.call_args_list))
        self.assertEqual(self.db.sql.call_args_list[-1].args[1][0],
                         tuple(sorted((*module.RAW_UOMS, *module.MENU, module.PORTION))))

    def test_snapshot_rejects_ambiguous_prior_baseline(self):
        baseline = self.state["previous"][0]
        duplicate = deepcopy(baseline)
        duplicate.name = "OTHER-BASELINE"
        self.db.sql.side_effect = [self.state["sles"], [baseline, duplicate]]
        with self.assertRaises(module.ProductionError):
            self.snapshot_function(CODES, WHEN)

    def test_snapshot_rejects_unsubmitted_or_foreign_voucher(self):
        for docstatus, company in ((0, module.COMPANY), (1, "Vox Lounge")):
            self.db.sql.side_effect = [self.state["sles"], *[[row] for row in self.state["previous"]]]
            self.frappe.get_doc.return_value = Doc(docstatus=docstatus, company=company)
            with self.subTest(status=docstatus), self.assertRaises(module.ProductionError):
                self.snapshot_function(CODES, WHEN)

    def test_baseline_negative_or_zero_quantity_nonzero_value_is_not_accepted(self):
        for quantity, value in ((-1, 40000), (0, 40000)):
            _made, _entries, self.state = fixture()
            self.state["previous"][0].qty_after_transaction = quantity
            self.state["previous"][0].stock_value = value
            with self.assertRaises(module.ProductionError):
                self.settle()
        self.update.assert_not_called()
