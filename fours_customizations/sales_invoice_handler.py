"""
Sales Invoice Handler — Fours Customizations.

After the salary-slip-only commission redesign, this module no longer books
any commission Journal Entries.  Its responsibilities are now:

  before_submit  → silently enable negative stock for OOS items
  before_save    → POS guards (warehouse, update_stock=0, advance allocation)
                   + draft Delivery Note check on returns
  on_submit      → create the draft Delivery Note, then auto-create + submit
                   a Sales Order for stock reservation (Req #1)

Commission earnings are computed in `salary_slip_handler.py` from GL Entries
and added straight to the Salary Slip.
"""

import frappe
from frappe.utils import flt, fmt_money
from frappe.model.mapper import get_mapped_doc

from fours_customizations.fours_customizations.doctype.four_s_industries_settings.four_s_industries_settings import (
        get_setting,
)


def _is_automation_enabled(company):
        """Check if selling automations are enabled for this company."""
        return frappe.db.get_value("Company", company, "enable_selling_automations")


def _default_pos_warehouse():
        """POS warehouse — used to override is_pos auto-set behaviour."""
        return get_setting("default_warehouse")


def _sync_custom_sales_person_to_team(doc):
        """When `custom_sales_person` is set, ensure the SI's `sales_team` table
        carries that person at 100% allocation.

        Idempotent: if the table already contains the same person at 100%, this is
        a no-op.  If a different person is in there, the table is replaced so the
        custom_sales_person is the single owner of this invoice.
        """
        partner = getattr(doc, "custom_sales_person", None)
        if not partner:
                return

        rate = flt(frappe.db.get_value("Sales Person", partner, "commission_rate"))
        allocated_amount = flt(doc.base_grand_total or doc.grand_total or 0)

        # If the table already has exactly this person at 100%, just refresh
        # the amount/rate and bail out.
        if len(doc.get("sales_team") or []) == 1:
                row = doc.sales_team[0]
                if row.sales_person == partner and flt(row.allocated_percentage) == 100:
                        row.allocated_amount = allocated_amount
                        row.commission_rate = rate
                        return

        # Otherwise, replace the table with a single 100% entry.
        doc.set("sales_team", [])
        doc.append("sales_team", {
                "sales_person": partner,
                "allocated_percentage": 100,
                "allocated_amount": allocated_amount,
                "commission_rate": rate,
                "incentives": 0,
        })


def _customer_available_credit(customer, company):
        """Company-currency advance/credit the customer holds on account — a net
        credit balance across their receivable accounts (they've paid ahead).
        Returns 0 when they owe on net. This invoice's own GL has not posted yet
        at before_submit, so this is the balance available to cover it.

        Assumes advances sit in the receivable account (Company /
        'Book Advance Payments in Separate Party Account' OFF, as configured here).
        """
        net = frappe.db.sql(
                """
                SELECT COALESCE(SUM(gle.debit - gle.credit), 0)
                FROM `tabGL Entry` gle
                INNER JOIN `tabAccount` a ON a.name = gle.account
                WHERE gle.party_type = 'Customer'
                    AND gle.party = %(customer)s
                    AND gle.company = %(company)s
                    AND gle.is_cancelled = 0
                    AND a.account_type = 'Receivable'
                """,
                {"customer": customer, "company": company},
        )[0][0]
        net = flt(net)
        return -net if net < 0 else 0.0


def _validate_payment_or_credit(doc):
        """Block submission unless the invoice is covered: paid now (POS / Include
        Payment + write-off), the customer holds enough available advance/credit,
        or the customer is whitelisted via custom_allow_credit."""
        # Consolidated POS invoices aggregate already-settled sales — never block.
        if doc.get("is_consolidated"):
                return

        # Whitelisted for credit → always allowed.
        if frappe.db.get_value("Customer", doc.customer, "custom_allow_credit"):
                return

        grand_total = flt(doc.rounded_total or doc.grand_total)
        if grand_total <= 0:
                return  # nothing to collect

        # Immediate payment recorded on this invoice (transaction currency).
        paid_now = flt(doc.paid_amount) + flt(doc.write_off_amount)

        # Customer's available credit is company-currency; convert to the invoice
        # currency (conversion_rate = company-currency units per invoice unit).
        conv = flt(doc.conversion_rate) or 1.0
        available_credit = _customer_available_credit(doc.customer, doc.company) / conv

        if paid_now + available_credit >= grand_total - 0.5:
                return

        shortfall = grand_total - paid_now - available_credit
        currency = doc.currency
        frappe.throw(
                f"""
<div style="font-family:'Segoe UI',Arial,sans-serif;line-height:1.6;color:#222;">
  <p style="font-size:14px;"><b>This invoice is not paid, and the customer is not cleared for credit.</b></p>
  <p><b>{doc.customer_name or doc.customer}</b> has neither paid in full nor holds enough
     advance/credit on account to cover it:</p>
  <table style="border-collapse:collapse;margin:8px 0 12px;font-size:13px;">
    <tr><td style="padding:4px 12px;border:1px solid #eee;">Invoice total</td>
        <td style="padding:4px 12px;border:1px solid #eee;text-align:right;"><b>{fmt_money(grand_total, currency=currency)}</b></td></tr>
    <tr><td style="padding:4px 12px;border:1px solid #eee;">Paid now</td>
        <td style="padding:4px 12px;border:1px solid #eee;text-align:right;">{fmt_money(paid_now, currency=currency)}</td></tr>
    <tr><td style="padding:4px 12px;border:1px solid #eee;">Available advance / credit</td>
        <td style="padding:4px 12px;border:1px solid #eee;text-align:right;">{fmt_money(available_credit, currency=currency)}</td></tr>
    <tr style="background:#fff5f5;"><td style="padding:4px 12px;border:1px solid #eee;"><b>Shortfall</b></td>
        <td style="padding:4px 12px;border:1px solid #eee;text-align:right;color:#c0392b;"><b>{fmt_money(shortfall, currency=currency)}</b></td></tr>
  </table>
  <p>To submit: collect full payment, ensure the customer has enough advance/credit on
     account, or tick <b>Allow Credit</b> on the customer if they are approved for credit.</p>
</div>
""",
                title="Payment or Credit Required",
        )


def before_submit(doc, method=None):
        doc.update_outstanding_for_self = 0
        # Silently enable negative stock on items that would otherwise block.
        if _is_automation_enabled(doc.company) and not doc.is_return:
                # Credit gate: block submission unless the invoice is paid, the
                # customer has enough advance/credit, or is whitelisted. Runs first
                # so we never spin up a Sales Order for an invoice we then reject.
                _validate_payment_or_credit(doc)

                try:
                        from fours_customizations.negative_stock_handler import ensure_negative_stock_for_doc

                        ensure_negative_stock_for_doc(doc)
                except Exception:
                        frappe.log_error(frappe.get_traceback(), "4S SI before_submit: negative stock check failed")

                # Create + submit the origin Sales Order and link the invoice items to
                # it BEFORE the billing pass, so ERPNext rolls the billed amount into
                # the Sales Order and the SO/DN cancel cleanly in reverse. (Previously
                # done in on_submit — after billing — which left the SO unbilled and
                # its links unregistered, blocking cancellation.)
                try:
                        from fours_customizations.si_to_so import create_sales_order_for_invoice

                        create_sales_order_for_invoice(doc)
                except Exception:
                        frappe.log_error(frappe.get_traceback(), "4S SI before_submit: SO creation failed")


def before_save(doc, method=None):
        """Auto-enable advance allocation and prevent Include Payment from forcing update_stock on."""
        if _is_automation_enabled(doc.company):
                doc.allocate_advances_automatically = 1
                if doc.is_pos:
                        doc.update_stock = 0
                        doc.set_posting_time = 1
                        pos_warehouse = _default_pos_warehouse()
                        if pos_warehouse:
                                doc.set_warehouse = pos_warehouse
                                for item in doc.items:
                                        item.warehouse = pos_warehouse

        # Sync custom_sales_person → sales_team child table at 100% allocation.
        _sync_custom_sales_person_to_team(doc)

        doc.update_outstanding_for_self = 0

        if doc.is_return or doc.return_against:
                dn_parents = frappe.get_all(
                        "Delivery Note Item",
                        filters={"against_sales_invoice": doc.return_against},
                        pluck="parent",
                )
                if not dn_parents:
                        return

                draft_dn = frappe.db.get_value(
                        "Delivery Note",
                        {"docstatus": 0, "name": ["in", dn_parents]},
                        "name",
                )
                if draft_dn:
                        full_name = frappe.db.get_value("User", frappe.session.user, "full_name") or "User"
                        name_parts = full_name.strip().split()
                        last_name = name_parts[-1] if len(name_parts) > 1 else full_name
                        frappe.throw(
                                f"{last_name} you make a cancellation request instead. Delivery Note {draft_dn} is still in draft."
                        )


def on_submit(doc, method=None):
        """Create the draft Delivery Note on submit.

        The origin Sales Order is created earlier, in before_submit, so the
        invoice can roll its billed amount into it. The Delivery Note is mapped
        off the invoice here and inherits against_sales_order / so_detail from the
        now-linked invoice items.
        """
        if not _is_automation_enabled(doc.company):
                return

        _create_draft_delivery_note(doc)


def _create_draft_delivery_note(doc):
        """Create a draft Delivery Note from the Sales Invoice."""
        if doc.update_stock:
                return

        if not doc.items:
                return

        # For normal invoices, skip if all items already have a DN link
        if not doc.is_return and all(getattr(item, "dn_detail", None) for item in doc.items):
                return

        # Only include items that maintain stock
        stock_item_codes = {
                row[0] for row in frappe.get_all(
                        "Item",
                        filters={
                                "name": ("in", [item.item_code for item in doc.items if item.item_code]),
                                "is_stock_item": 1,
                        },
                        fields=["name"],
                        as_list=True,
                )
        }

        if not stock_item_codes:
                return

        if doc.is_return:
                _create_draft_delivery_note_return(doc, stock_item_codes)
                return

        # Normal invoice flow
        has_eligible_items = any(
                item.item_code in stock_item_codes
                and not getattr(item, "dn_detail", None)
                and flt(item.qty) - flt(item.delivered_qty) > 0
                and not item.get("delivered_by_supplier")
                for item in doc.items
        )
        if not has_eligible_items:
                return

        def set_missing_values(source, target):
                target.run_method("set_missing_values")
                target.run_method("set_po_nos")
                target.run_method("calculate_taxes_and_totals")

        def update_item(source_doc, target_doc, source_parent):
                target_doc.qty = flt(source_doc.qty) - flt(source_doc.delivered_qty)
                target_doc.stock_qty = target_doc.qty * flt(source_doc.conversion_factor)
                target_doc.base_amount = target_doc.qty * flt(source_doc.base_rate)
                target_doc.amount = target_doc.qty * flt(source_doc.rate)

        dn = get_mapped_doc(
                "Sales Invoice",
                doc.name,
                {
                        "Sales Invoice": {
                                "doctype": "Delivery Note",
                                "validation": {"docstatus": ["=", 1]},
                        },
                        "Sales Invoice Item": {
                                "doctype": "Delivery Note Item",
                                "field_map": {
                                        "name": "si_detail",
                                        "parent": "against_sales_invoice",
                                        "serial_no": "serial_no",
                                        "sales_order": "against_sales_order",
                                        "so_detail": "so_detail",
                                        "cost_center": "cost_center",
                                },
                                "postprocess": update_item,
                                "condition": lambda item: (
                                        item.item_code in stock_item_codes
                                        and not item.get("delivered_by_supplier")
                                        and not getattr(item, "dn_detail", None)
                                        and flt(item.qty) - flt(item.delivered_qty) > 0
                                ),
                        },
                        "Sales Taxes and Charges": {
                                "doctype": "Sales Taxes and Charges",
                                "reset_value": True,
                        },
                        "Sales Team": {
                                "doctype": "Sales Team",
                                "field_map": {"incentives": "incentives"},
                                "add_if_empty": True,
                        },
                },
                None,
                set_missing_values,
        )

        if dn and dn.items:
                dn.set_posting_time = 1
                dn.posting_date = doc.posting_date
                dn.posting_time = doc.posting_time
                dn.insert(ignore_permissions=True)
                frappe.msgprint(f"Draft Delivery Note {dn.name} created.", alert=True)


def _create_draft_delivery_note_return(doc, stock_item_codes):
        """Create a draft Delivery Note Return from a return Sales Invoice."""
        if not doc.return_against:
                return

        existing_dn = frappe.db.get_value(
                "Delivery Note",
                {
                        "docstatus": 0,
                        "is_return": 1,
                        "return_against": ["is", "set"],
                        "custom_remarks": ["like", f"%Auto-created from Sales Invoice Return {doc.name}%"],
                },
                "name",
        )
        if existing_dn:
                return

        returned_items = [
                item for item in doc.items
                if item.item_code in stock_item_codes
                and not item.get("delivered_by_supplier")
                and flt(item.qty) < 0
                and getattr(item, "sales_invoice_item", None)
        ]

        if not returned_items:
                return

        original_si_item_names = [item.sales_invoice_item for item in returned_items if item.sales_invoice_item]
        if not original_si_item_names:
                return

        dn_items = frappe.get_all(
                "Delivery Note Item",
                filters={
                        "docstatus": 1,
                        "si_detail": ["in", original_si_item_names],
                },
                fields=[
                        "name",
                        "parent",
                        "item_code",
                        "rate",
                        "warehouse",
                        "cost_center",
                        "si_detail",
                        "against_sales_invoice",
                        "uom",
                        "stock_uom",
                        "conversion_factor",
                ],
        )

        if not dn_items:
                return

        dn_item_map = {}
        for row in dn_items:
                dn_item_map.setdefault(row.parent, []).append(row)

        returned_si_item_map = {
                item.sales_invoice_item: item
                for item in returned_items
                if item.sales_invoice_item
        }

        created_dns = []

        for original_dn_name, original_dn_items in dn_item_map.items():
                already_exists = frappe.db.get_value(
                        "Delivery Note",
                        {
                                "docstatus": 0,
                                "is_return": 1,
                                "return_against": original_dn_name,
                                "custom_remarks": ["like", f"%Auto-created from Sales Invoice Return {doc.name}%"],
                        },
                        "name",
                )
                if already_exists:
                        created_dns.append(already_exists)
                        continue

                original_dn = frappe.get_doc("Delivery Note", original_dn_name)

                dn_return = frappe.new_doc("Delivery Note")
                dn_return.is_return = 1
                dn_return.return_against = original_dn.name
                dn_return.naming_series = original_dn.naming_series
                dn_return.customer = original_dn.customer
                dn_return.customer_name = original_dn.customer_name
                dn_return.company = original_dn.company
                dn_return.posting_date = doc.posting_date
                dn_return.posting_time = doc.posting_time
                dn_return.set_posting_time = 1
                dn_return.currency = original_dn.currency
                dn_return.conversion_rate = original_dn.conversion_rate
                dn_return.selling_price_list = original_dn.selling_price_list
                dn_return.price_list_currency = original_dn.price_list_currency
                dn_return.plc_conversion_rate = original_dn.plc_conversion_rate
                dn_return.ignore_pricing_rule = 1
                dn_return.set_warehouse = original_dn.set_warehouse
                dn_return.cost_center = original_dn.cost_center
                dn_return.company_address = original_dn.company_address
                dn_return.sales_partner = original_dn.sales_partner
                dn_return.letter_head = original_dn.letter_head
                dn_return.custom_creator_name = getattr(original_dn, "custom_creator_name", None)
                dn_return.custom_employee_name = getattr(original_dn, "custom_employee_name", None)
                dn_return.custom_approver_name = getattr(original_dn, "custom_approver_name", None)
                dn_return.custom_deliverer_name = getattr(original_dn, "custom_deliverer_name", None)
                dn_return.custom_remarks = f"Auto-created from Sales Invoice Return {doc.name}"

                for dn_item in original_dn_items:
                        si_return_item = returned_si_item_map.get(dn_item.si_detail)
                        if not si_return_item:
                                continue

                        dn_return.append("items", {
                                "item_code": si_return_item.item_code,
                                "item_name": si_return_item.item_name,
                                "description": si_return_item.description,
                                "qty": flt(si_return_item.qty),
                                "uom": si_return_item.uom or dn_item.uom,
                                "stock_uom": si_return_item.stock_uom or dn_item.stock_uom,
                                "conversion_factor": flt(si_return_item.conversion_factor or dn_item.conversion_factor or 1),
                                "warehouse": dn_item.warehouse or si_return_item.warehouse or original_dn.set_warehouse,
                                "rate": flt(dn_item.rate or si_return_item.rate),
                                "price_list_rate": flt(dn_item.rate or si_return_item.price_list_rate or si_return_item.rate),
                                "cost_center": dn_item.cost_center or original_dn.cost_center,
                                "against_sales_invoice": doc.return_against,
                                "si_detail": dn_item.si_detail,
                                "dn_detail": dn_item.name,
                                "use_serial_batch_fields": getattr(si_return_item, "use_serial_batch_fields", 0),
                        })

                if not dn_return.items:
                        continue

                dn_return.flags.ignore_permissions = True
                dn_return.insert()
                created_dns.append(dn_return.name)

        if created_dns:
                frappe.msgprint(
                        f"Draft Delivery Note Return {', '.join(created_dns)} created.",
                        alert=True,
                )
