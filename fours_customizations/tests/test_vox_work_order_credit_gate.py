"""Standalone tests of the narrow credit branch in native goat production.

Only the private credit predicate is mocked here. Its durable-approval and
object-capability checks belong to the separately owned credit-module tests.
The real _trusted_source gate and its existing POS/source rules run unchanged.
"""

from copy import deepcopy
from types import ModuleType, SimpleNamespace
import sys
from unittest import TestCase
from unittest.mock import Mock, patch

from fours_customizations import vox_work_orders as module


class Doc(SimpleNamespace):
    def get(self, name, default=None):
        return self.__dict__.get(name, default)

    def as_dict(self):
        return deepcopy(self.__dict__)


class TestVoxWorkOrderCreditGate(TestCase):
    def setUp(self):
        from kit_pos.connector import materialize

        self.db = SimpleNamespace(commit=Mock(), has_column=Mock(return_value=False))
        self.frappe = SimpleNamespace(db=self.db, local=SimpleNamespace(site=module.SITE),
                                     session=SimpleNamespace(user="Administrator"),
                                     conf={module.CONFIG: {module.COMPANY: True}},
                                     get_all=Mock(return_value=[Doc(name=4929)]), get_doc=Mock())
        self.enterContext(patch.object(module, "frappe", self.frappe))
        self.credit = ModuleType("fours_customizations.vox_pilot_credit")
        self.approved = self.credit.is_exact_approved_credit = Mock(return_value=True)
        self.enterContext(patch.dict(sys.modules, {"fours_customizations.vox_pilot_credit": self.credit}))
        self.invoice = Doc(doctype="Sales Invoice", name=module.PILOT["invoice"], company=module.COMPANY,
                           is_pos=0, is_created_using_pos=0, is_return=0, update_stock=1,
                           customer="Walk-In Customer", currency="UGX", conversion_rate=1,
                           pos_profile="Main Bar", set_warehouse="Main counter - VXN",
                           posting_date="2026-09-28", posting_time="23:09:40", grand_total=54000,
                           paid_amount=0, payments=[],
                           items=[Doc(item_code="VXN-PAN-FRIED-GOAT", warehouse=module.WAREHOUSE)])
        self.sale = Doc(name=4929, sale_uuid=module.PILOT["sale_uuid"], sales_invoice=self.invoice.name,
                        invoice_no="KOD-2-101", status="final", is_suspend=0, is_quotation=0,
                        payment_status="due", amount_paid=0, final_total=54000, payments=[],
                        transaction_date="2026-09-28 23:09:40")
        self.location = Doc(name="Main Bar", company=module.COMPANY, warehouse="Main counter - VXN",
                            selling_price_list="Standard Selling", pos_profile="Main Bar")
        self.context = Doc(invoice=self.invoice, sale=self.sale, location=self.location,
                           user=self.frappe.session.user)
        self.native_context = self.enterContext(patch.object(materialize, "_invoice_submission_context",
                                                             side_effect=lambda invoice: self.context))
        self.source_lock = self.enterContext(patch.object(materialize, "_locked_sale", side_effect=lambda _: self.sale))
        self.finalized = self.enterContext(patch.object(materialize, "_reject_finalized_opening"))
        self.enterContext(patch.object(materialize, "_location_for", side_effect=lambda _: self.location))
        self.enterContext(patch.object(materialize, "_pos_profile_for", return_value="Main Bar"))
        self.enterContext(patch.object(materialize, "_customer_for", return_value="Walk-In Customer"))
        self.enterContext(patch.object(materialize, "_posting", return_value={"posting_date": "2026-09-28",
                                                                            "posting_time": "23:09:40"}))

    def _refused(self):
        with self.assertRaises(module.ProductionError):
            module._trusted_source(self.invoice)
        self.db.commit.assert_not_called()

    def _paid_pos(self):
        self.invoice.is_pos = self.invoice.is_created_using_pos = 1
        self.invoice.paid_amount = self.sale.amount_paid = 54000
        self.invoice.payments = [Doc(amount=54000)]
        self.sale.payments = [Doc(amount=54000, is_return=0)]
        self.sale.payment_status = "paid"

    def test_exact_approved_credit_uses_flags00_due_zero_paid_and_no_payment_rows(self):
        before = self.invoice.as_dict(), self.sale.as_dict()
        self.assertEqual(module._trusted_source(self.invoice), (self.sale, self.location))
        self.approved.assert_called_once_with(self.invoice, self.sale, self.location)
        self.assertIs(self.approved.call_args.args[0], self.invoice)
        self.assertIs(self.approved.call_args.args[1], self.sale)
        self.assertEqual((self.invoice.as_dict(), self.sale.as_dict()), before)
        self.source_lock.assert_called_once_with("4929")
        self.db.commit.assert_not_called()

    def test_native_blank_header_accepts_exact_kitchen_and_bar_rows(self):
        self.invoice.set_warehouse = None
        self.invoice.items = [Doc(item_code="VXN-PAN-FRIED-GOAT", warehouse=module.WAREHOUSE),
                              Doc(item_code="VX-0017", warehouse=self.location.warehouse)]
        self.assertEqual(module._trusted_source(self.invoice), (self.sale, self.location))
        self._paid_pos()
        self.assertEqual(module._trusted_source(self.invoice), (self.sale, self.location))

    def test_bar_header_accepts_correctly_mixed_credit_and_paid_pos_invoices(self):
        self.invoice.items.append(Doc(item_code="VX-0017", warehouse=self.location.warehouse))
        self.assertEqual(module._trusted_source(self.invoice), (self.sale, self.location))
        self._paid_pos()
        self.assertEqual(module._trusted_source(self.invoice), (self.sale, self.location))

    def test_paid_pos_bar_header_rejects_every_misrouted_drink_warehouse(self):
        self._paid_pos()
        for warehouse in (module.WAREHOUSE, "Other Warehouse", None, ""):
            self.invoice.items = [Doc(item_code="VXN-PAN-FRIED-GOAT", warehouse=module.WAREHOUSE),
                                  Doc(item_code="VX-0017", warehouse=warehouse)]
            with self.subTest(warehouse=warehouse):
                self._refused()
        self.approved.assert_not_called()

    def test_bar_header_rejects_each_dedicated_goat_outside_kitchen(self):
        for code in module.MENU:
            for warehouse in (self.location.warehouse, "Other Warehouse", None, ""):
                self.invoice.items = [Doc(item_code=code, warehouse=warehouse),
                                      Doc(item_code="VX-0017", warehouse=self.location.warehouse)]
                with self.subTest(item=code, warehouse=warehouse):
                    self._refused()

    def test_bar_header_preserves_single_and_multiple_goat_paid_pos_invoices(self):
        self._paid_pos()
        for code in module.MENU:
            self.invoice.items = [Doc(item_code=code, warehouse=module.WAREHOUSE)]
            with self.subTest(item=code):
                self.assertEqual(module._trusted_source(self.invoice), (self.sale, self.location))
        self.invoice.items = [Doc(item_code=code, warehouse=module.WAREHOUSE) for code in module.MENU]
        self.assertEqual(module._trusted_source(self.invoice), (self.sale, self.location))
        self.approved.assert_not_called()

    def test_bar_header_preserves_ordinary_bar_item_paid_pos_invoices(self):
        self._paid_pos()
        self.invoice.items = [Doc(item_code=code, warehouse=self.location.warehouse)
                              for code in ("VX-0017", "VX-0128")]
        self.assertEqual(module._trusted_source(self.invoice), (self.sale, self.location))
        self.approved.assert_not_called()

    def test_matching_header_cannot_hide_absent_rows_or_source_bar(self):
        self.invoice.items = []
        self._refused()
        self.invoice.items = [Doc(item_code="VXN-PAN-FRIED-GOAT", warehouse=module.WAREHOUSE)]
        for warehouse in (None, ""):
            self.invoice.set_warehouse = self.location.warehouse = warehouse
            with self.subTest(warehouse=warehouse):
                self.assertFalse(module._invoice_warehouse_header_matches(self.invoice, self.location))
                self._refused()

    def test_blank_header_still_rejects_uniform_kitchen_or_bar_rows(self):
        for header in (None, ""):
            self.invoice.set_warehouse = header
            for rows in ([Doc(item_code="VXN-PAN-FRIED-GOAT", warehouse=module.WAREHOUSE)],
                         [Doc(item_code="VX-0017", warehouse=self.location.warehouse)]):
                self.invoice.items = rows
                with self.subTest(header=header, warehouse=rows[0].warehouse):
                    self._refused()

    def test_native_blank_header_never_accepts_wrong_or_swapped_item_warehouses(self):
        self.invoice.set_warehouse = None
        for goat_wh, drink_wh in ((self.location.warehouse, module.WAREHOUSE),
                                  (module.WAREHOUSE, "Other Warehouse"),
                                  (module.WAREHOUSE, None), (module.WAREHOUSE, module.WAREHOUSE)):
            self.invoice.items = [Doc(item_code="VXN-PAN-FRIED-GOAT", warehouse=goat_wh),
                                  Doc(item_code="VX-0017", warehouse=drink_wh)]
            with self.subTest(goat=goat_wh, drink=drink_wh):
                self._refused()

    def test_blank_header_requires_an_actual_mixed_invoice_and_never_a_foreign_default(self):
        self.invoice.items = [Doc(item_code="VXN-PAN-FRIED-GOAT", warehouse=module.WAREHOUSE),
                              Doc(item_code="VX-0017", warehouse=self.location.warehouse)]
        self.invoice.set_warehouse = "Other Warehouse"
        self._refused()
        self.invoice.set_warehouse = None
        self.invoice.items = []
        self._refused()

    def test_unsigned_credit_flags_cannot_authorize_even_with_native_link_or_forged_document_flags(self):
        self.approved.return_value = False
        self.invoice.flags = Doc(approved_credit=True, kit_pos_sale_id=4929, ignore_permissions=True)
        self._refused()
        self.context = None
        self._refused()
        self.assertEqual(self.approved.call_count, 2)

    def test_credit_predicate_must_return_literal_true_not_merely_truthy(self):
        for value in (False, None, 1, "true", "approved", {}, object()):
            self.approved.return_value = value
            with self.subTest(value=repr(value)):
                self._refused()

    def test_mixed_missing_or_textual_zero_flags_do_not_enter_credit_path(self):
        for is_pos, created in ((1, 0), (0, 1), (None, 0), (0, None), ("", 0), (0, ""), ("0", 0), (0, "0")):
            self.invoice.is_pos, self.invoice.is_created_using_pos = is_pos, created
            with self.subTest(is_pos=is_pos, created=created):
                self._refused()
        self.approved.assert_not_called()

    def test_ordinary_fully_paid_pos_never_calls_credit_predicate(self):
        self._paid_pos()
        self.approved.side_effect = AssertionError("Paid POS must not consult credit approval.")
        self.assertEqual(module._trusted_source(self.invoice), (self.sale, self.location))
        self.approved.assert_not_called()
        self.sale.amount_paid = 60000
        self.sale.payments = [Doc(amount=60000, is_return=0), Doc(amount=6000, is_return=1)]
        self.assertEqual(module._trusted_source(self.invoice), (self.sale, self.location))

    def test_unpaid_pos_flags11_do_not_fall_through_to_approved_credit(self):
        self.invoice.is_pos = self.invoice.is_created_using_pos = 1
        self._refused()
        self.approved.assert_not_called()

    def test_suspended_or_paid_credit_source_is_rejected(self):
        self.sale.is_suspend = 1
        self._refused()
        self.sale.is_suspend = "0"
        self._refused()
        self.sale.is_suspend = 0
        for status in ("paid", "partial", "", None):
            self.sale.payment_status = status
            with self.subTest(status=status):
                self._refused()

    def test_any_source_or_invoice_payment_row_is_rejected_even_zero_or_change_only(self):
        for target in (self.sale, self.invoice):
            for payments in ([Doc(amount=0)], [Doc(amount=0, is_return=1)], [Doc(amount=54000, is_return=1)]):
                target.payments = payments
                with self.subTest(target="sale" if target is self.sale else "invoice", payments=payments):
                    self._refused()
            target.payments = []

    def test_credit_requires_exact_zero_on_both_paid_amount_headers(self):
        for target in (self.sale, self.invoice):
            field = "paid_amount" if target is self.invoice else "amount_paid"
            for value in (1, 54000, -1, None, "NaN", "Infinity", True, "0.0000001"):
                setattr(target, field, value)
                with self.subTest(target="sale" if target is self.sale else "invoice", value=value):
                    self._refused()
            setattr(target, field, 0)

    def test_approved_credit_still_requires_company_customer_currency_stock_and_profile_rules(self):
        fields = (("company", "4S Industries Limited"), ("customer", "Another Customer"), ("currency", "USD"),
                  ("conversion_rate", 3500), ("update_stock", 0), ("is_return", 1), ("doctype", "POS Invoice"),
                  ("pos_profile", "Mini Bar"), ("set_warehouse", "Other Warehouse"),
                  ("posting_date", "2026-09-29"), ("posting_time", "23:09:41"), ("grand_total", 53000))
        for field, value in fields:
            previous = self.invoice.get(field)
            setattr(self.invoice, field, value)
            with self.subTest(field=field):
                self._refused()
            setattr(self.invoice, field, previous)

    def test_approved_credit_still_rejects_void_merge_quote_draft_and_invalid_uuid(self):
        for field, value in (("deleted_at", "2026-09-29"), ("voided_bill", "VOID-1"), ("merged_into", "OTHER"),
                             ("is_quotation", 1), ("status", "draft"), ("sale_uuid", "invalid")):
            previous = self.sale.get(field)
            setattr(self.sale, field, value)
            with self.subTest(field=field):
                self._refused()
            setattr(self.sale, field, previous)

    def test_credit_approval_does_not_bypass_missing_conflicting_or_ambiguous_native_source(self):
        self.context = None
        self.frappe.get_all.return_value = []
        self._refused()
        self.frappe.get_all.return_value = [Doc(name=4929), Doc(name=2128)]
        self._refused()
        self.context = Doc(invoice=self.invoice, sale=self.sale, location=self.location, user="Administrator")
        self.frappe.get_all.return_value = [Doc(name=2128)]
        self._refused()
        self.approved.assert_not_called()

    def test_finalized_opening_is_rejected_before_credit_exception(self):
        self.finalized.side_effect = module.ProductionError("Finalized opening")
        self._refused()
        self.approved.assert_not_called()

    def test_other_invoice_or_sale_does_not_gain_credit_when_exact_predicate_declines(self):
        self.approved.return_value = False
        self.invoice.name = self.sale.sales_invoice = "SI-OTHER"
        self.sale.name = 9999
        self.sale.sale_uuid = "18d310b5-8c03-4ce7-9503-8e54e975d81f"
        self.frappe.get_all.return_value = [Doc(name=9999)]
        self._refused()
        self.approved.assert_called_once_with(self.invoice, self.sale, self.location)

    def test_unrelated_company_site_and_disabled_feature_do_not_enter_production(self):
        with patch.object(module, "_ensure_invoice_production") as engine:
            self.invoice.company = "4S Industries Limited"
            module.ensure_invoice_production(self.invoice)
            self.invoice.company = module.COMPANY
            self.frappe.local.site = "ddamu.kit.africa"
            module.ensure_invoice_production(self.invoice)
            self.frappe.local.site = module.SITE
            self.frappe.conf = {}
            module.ensure_invoice_production(self.invoice)
            engine.assert_not_called()
        self.approved.assert_not_called()

    def test_existing_paid_pos_failure_rules_are_not_relaxed(self):
        self._paid_pos()
        for field, value in (("is_suspend", 1), ("payment_status", "due"), ("amount_paid", 0),
                             ("final_total", 60000), ("payments", [])):
            previous = self.sale.get(field)
            setattr(self.sale, field, value)
            with self.subTest(field=field):
                self._refused()
            setattr(self.sale, field, previous)
        self.approved.assert_not_called()
