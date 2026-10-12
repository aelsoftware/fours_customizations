"""Private, exact held-bill correction approved for Vox sale 4929 only.

preview() is read-only. apply_correction() owns no transaction boundaries:
the caller must review/backup, commit success or roll back ANY exception.
No method is whitelisted. No payment, stock production, invoice submission,
global policy change or fabricated till/PIN approval belongs in this helper.
"""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from uuid import UUID, uuid5

import frappe
from frappe.utils import get_datetime, get_time, getdate


SITE = "multax.kit.africa"
COMPANY = "Vox Lounge Nansana"
SALE = "4929"
SALE_UUID = "a0812a30-f0c7-443d-803b-3115249811c2"
INVOICE = "SI-2640-198"
LABEL = "KOD-2-101"
SOLD_AT = datetime(2026, 9, 28, 23, 9, 40)
LOCATION = "Main Bar"
LOCATION_ID = 1996
OPENING = "POS-OPE-2026-00003"
BAR_WAREHOUSE = "Main counter - VXN"
KITCHEN = "Kitchen - VXN"
OLD_LINE_ID = 15806
OLD_LINE_UUID = "28d310b5-8c03-4ce7-9503-8e54e975d81f"
OLD_ITEM = "VX-0128"
NEW_ITEM = "VXN-PAN-FRIED-GOAT"
NEW_LINE_UUID = str(uuid5(UUID(SALE_UUID),
    "vox-pilot-correction:v1:28d310b5-8c03-4ce7-9503-8e54e975d81f:VXN-PAN-FRIED-GOAT:Chips:54000"))
NEW_LINE_NO = "VXN-PILOT-CORRECTION-" + NEW_LINE_UUID.replace("-", "")
OLD_TOTAL = 49000
NEW_TOTAL = 54000
PLANNING_WORK_ORDER = "MFG-WO-2026-00001"
MODIFIER_FIELD = "custom_kit_pos_modifier_reference"
PRODUCTION_KEY = "custom_vox_production_key"
WORK_ORDER_FIELD = "custom_vox_work_order"
TOMBSTONES = "removed_line_tombstones"
RETAINED = (
    (15807, "cc65b866-40a9-4490-bfd3-5c1e041ee59c", "VX-0017", 664, 1, 6000),
    (15808, "a712e02e-acd7-4829-8957-08bc17c2862e", "VX-0020", 661, 1, 5000),
    (15809, "4031ecf7-60f6-41af-87b7-6024aeb96279", "VX-0033", 648, 1, 3000),
    (15810, "45292af1-9f03-4216-a6f7-88355a878811", "VX-0012", 669, 2, 5000),
)
_INVOICE_DERIVED = {
    "items", "modified", "modified_by", "total", "base_total", "net_total", "base_net_total",
    "grand_total", "base_grand_total", "rounded_total", "base_rounded_total", "outstanding_amount",
    "in_words", "base_in_words", "total_net_weight", "amount_eligible_for_commission",
    # Native ERPNext clears this form default for mixed kitchen/bar rows;
    # exact per-item warehouses are independently verified below.
    "set_warehouse",
}
_INVOICE_ITEM_BUSINESS = (
    "idx", "item_code", "qty", "rate", "amount", "net_rate", "net_amount", "base_rate", "base_amount",
    "base_net_rate", "base_net_amount", "uom", "stock_uom", "conversion_factor", "stock_qty", "warehouse",
    "income_account", "expense_account", "cost_center", "item_tax_template", "sales_order", "so_detail",
    "delivery_note", "dn_detail", MODIFIER_FIELD, PRODUCTION_KEY, WORK_ORDER_FIELD,
)


class CorrectionError(ValueError):
    pass


def _require(condition, message="The exact approved held bill changed; obtain a new management review."):
    if not condition:
        raise CorrectionError(message)


def _number(value):
    _require(not isinstance(value, bool) and value not in (None, ""))
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise CorrectionError("Invalid amount or identity in the approved bill.") from None
    _require(result.is_finite())
    return result


def _id(value):
    _require(type(value) in (str, int))
    result = _number(value)
    _require(result > 0 and result == result.to_integral_value() and result < 2**53)
    _require(not isinstance(value, str) or value == str(int(result)))
    return int(result)


def _dict(document):
    converter = getattr(document, "as_dict", None)
    return deepcopy(converter() if callable(converter) else dict(document))


def _json(raw):
    try:
        result = json.loads(raw) if isinstance(raw, str) else deepcopy(raw)
    except (TypeError, ValueError):
        raise CorrectionError("The original bill payload is invalid.") from None
    _require(isinstance(result, dict))
    return result


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _properties(value, omitted=()):
    return {key: raw for key, raw in _dict(value).items()
            if key not in omitted and not key.startswith("__")}


def _guard():
    _require(frappe.local.site == SITE and frappe.session.user == "Administrator",
             "This private correction requires Administrator on Multax.")
    _require(not getattr(frappe.db, "auto_commit_on_many_writes", False)
             and not getattr(frappe.db, "_disable_transaction_control", False),
             "The caller must retain normal transaction control.")


def _access(sale):
    from kit_pos.connector.sale_permissions import PosAccess

    access = PosAccess()
    access.require("sell.update", "correcting this confirmed held bill")
    # The location's auto-hold policy is OFF: prepare_update does not grant
    # this removal itself. Require management authority explicitly first.
    access.require("sell.remove_line_items", "removing the confirmed old goat line")
    access.require_sale(sale, SALE)
    access.require_location(LOCATION_ID)
    _require(access.user == "Administrator")
    return access


def _existing_identity(doctype, name):
    from kit_pos.connector import ids

    value = frappe.db.get_value("Kit POS Id", {"ref_doctype": doctype, "ref_name": name}, "name")
    _require(value is not None, "The approved native POS identity must already exist.")
    identity = _id(value)
    _require(ids.lookup(doctype, identity) == name, "An approved POS identity was redirected or changed.")
    return identity


def _setup(location):
    from kit_pos.connector import ids, item_uom, sales
    from fours_customizations import vox_modifiers, vox_work_orders

    item = frappe.get_doc("Item", NEW_ITEM)
    _require(item.disabled == 0 and item.is_stock_item == 1 and item.is_sales_item == 1
             and item.stock_uom == "Pc(s)")
    product = _existing_identity("Item", NEW_ITEM)
    unit = _existing_identity("UOM", "Pc(s)")
    uom, factor = item_uom.selected_uom(NEW_ITEM, "Pc(s)", unit,
        client_multiplier=1, client_unit_name="Pc(s)")
    _require(uom == "Pc(s)" and _number(factor) == 1)
    price = item_uom.selling_price_for_uom(NEW_ITEM, "Pc(s)", standard_rate=None,
        price_list=location.get("selling_price_list"), uom=uom, conversion_factor=factor, on_date=SOLD_AT)
    _require(_number(price) == 20000, "The approved Pan Fried Goat selling price changed.")
    public = vox_modifiers.provider(operation="catalogue", company=COMPANY, location=location,
                                    item_codes=[NEW_ITEM])
    choices = [choice for group in public.get(NEW_ITEM, []) if group["name"] == "Sides"
               for choice in group["modifiers"] if choice["name"] == "Chips"]
    _require(len(choices) == 1 and _number(choices[0]["price"]) == 10000)
    for _line, _uuid, code, identity, _qty, _rate in RETAINED:
        _require(ids.lookup("Item", identity) == code)
    _require(ids.lookup("Item", 554) == OLD_ITEM)
    key = sales._line_registry_key(SALE, NEW_LINE_UUID, NEW_LINE_NO)
    new_line_id = frappe.db.get_value("Kit POS Id", {"ref_doctype": "Kit POS Sale Line", "ref_name": key}, "name")
    return {"product_id": product, "uom_id": unit, "modifier_id": _id(choices[0]["variation_id"]),
            "new_line_id": _id(new_line_id) if new_line_id is not None else None,
            "new_production_key": vox_work_orders.production_key(SALE_UUID, NEW_LINE_UUID),
            "item": _dict(item), "modifier": choices[0]}


def _read_state(*, locked=False):
    from kit_pos.connector import materialize
    from fours_customizations import vox_work_orders

    sale = materialize._locked_sale(SALE) if locked else frappe.get_doc("Kit POS Sale", SALE)
    if locked:
        materialize._reject_finalized_opening(sale)
    invoice = frappe.get_doc("Sales Invoice", INVOICE, for_update=locked)
    location = materialize._location_for(sale)
    _require(location and location.get("name") == LOCATION and location.get("company") == COMPANY
             and location.get("warehouse") == BAR_WAREHOUSE and materialize._pos_profile_for(location) == LOCATION)
    setup = _setup(location)
    work_order = frappe.get_doc("Work Order", PLANNING_WORK_ORDER)
    vox_work_orders._validate_pilot_retirement_work_order(work_order,
        state="cancelled" if work_order.docstatus == 2 else "submitted")
    # No pending/replayed production may exist under the replacement's key.
    effects = {
        "replacement_work_orders": frappe.db.count("Work Order", {PRODUCTION_KEY: setup["new_production_key"]}),
        "stock_entries": frappe.db.count("Stock Entry", {"work_order": PLANNING_WORK_ORDER}),
        "job_cards": frappe.db.count("Job Card", {"work_order": PLANNING_WORK_ORDER}),
        "invoice_stock_ledgers": frappe.db.count("Stock Ledger Entry", {"voucher_type": "Sales Invoice", "voucher_no": INVOICE}),
        "invoice_gl": frappe.db.count("GL Entry", {"voucher_type": "Sales Invoice", "voucher_no": INVOICE}),
        "allocated_payments": frappe.db.count("Payment Entry Reference", {"reference_doctype": "Sales Invoice", "reference_name": INVOICE}),
        "allocated_journals": frappe.db.count("Journal Entry Account", {"reference_type": "Sales Invoice", "reference_name": INVOICE}),
    }
    return {"sale": sale, "invoice": invoice, "location": location,
            "setup": setup, "work_order": work_order, "effects": effects}


def _check_line(row, *, line_id, uuid, code, product, qty, rate):
    _require(_id(row.get("line_id")) == line_id and row.get("desktop_line_uuid") == uuid
             and row.get("item_code") == code and _id(row.get("product_id")) == product
             and _id(row.get("variation_id")) == product and _number(row.get("quantity")) == qty)
    _require(all(_number(row.get(field)) == rate
                 for field in ("unit_price", "unit_price_before_discount", "unit_price_inc_tax")))
    _require(_id(row.get("sub_unit_id")) == 1254 and _number(row.get("item_tax") or 0) == 0
             and _number(row.get("line_discount_amount") or 0) == 0
             and not row.get("parent_sell_line_id") and not row.get("children_type"))


def _check_invoice_row(row, code, qty, rate, *, kitchen=False):
    _require(row.get("item_code") == code and _number(row.get("qty")) == qty
             and _number(row.get("rate")) == rate and _number(row.get("amount")) == qty * rate
             and row.get("uom") == row.get("stock_uom") == "Pc(s)"
             and _number(row.get("conversion_factor")) == 1
             and row.get("warehouse") == (KITCHEN if kitchen else BAR_WAREHOUSE)
             and not row.get(PRODUCTION_KEY) and not row.get(WORK_ORDER_FIELD))


def _check_payload(sale, corrected):
    payload = _json(sale.get("payload"))
    _require(payload.get("uuid") == SALE_UUID and payload.get("invoice_no") == LABEL
             and _id(payload.get("location_id")) == LOCATION_ID and payload.get("cash_register_id") == OPENING
             and _id(payload.get("created_by")) == 2210 and _id(payload.get("service_staff_id")) == 2051
             and _id(payload.get("table_id")) == 2136 and _id(payload.get("contact_id")) == 1265
             and get_datetime(payload.get("transaction_date")) == SOLD_AT
             and payload.get("status") == "final" and _number(payload.get("is_suspend")) == 1
             and not payload.get("payments") and not payload.get("line_removal_approvals")
             and _number(payload.get("final_total")) == (NEW_TOTAL if corrected else OLD_TOTAL))
    products = payload.get("products")
    rows = sale.get("lines") or []
    _require(isinstance(products, list) and len(products) == len(rows) == 5)
    for product, row in zip(products, rows, strict=True):
        _require(isinstance(product, dict) and product.get("desktop_line_uuid") == row.get("desktop_line_uuid")
                 and _id(product.get("product_id")) == _id(row.get("product_id"))
                 and _id(product.get("variation_id")) == _id(row.get("variation_id"))
                 and _number(product.get("quantity")) == _number(row.get("quantity"))
                 and _number(product.get("unit_price_inc_tax")) == _number(row.get("unit_price_inc_tax")))
    _require(set(payload.get("removed_line_ids") or []) == ({OLD_LINE_ID} if corrected else set())
             and set(payload.get("removed_line_uuids") or []) == ({OLD_LINE_UUID} if corrected else set()))
    return payload


def _validate_state(state):
    from kit_pos.connector import auto_held_lines
    from fours_customizations import vox_modifiers

    sale, invoice, setup = state["sale"], state["invoice"], state["setup"]
    lines, invoice_rows = sale.get("lines") or [], invoice.get("items") or []
    _require(len(lines) == len(invoice_rows) == 5)
    corrected = lines[0].get("desktop_line_uuid") == NEW_LINE_UUID
    _require(_id(sale.get("name")) == int(SALE) and sale.get("sale_uuid") == SALE_UUID
             and sale.get("invoice_no") == LABEL and get_datetime(sale.get("transaction_date")) == SOLD_AT
             and _id(sale.get("location_id")) == LOCATION_ID and sale.get("location") == LOCATION
             and sale.get("pos_opening_entry") == OPENING and sale.get("sales_invoice") == INVOICE
             and _id(sale.get("created_by_id")) == 2210 and _id(sale.get("waiter_id")) == 2051
             and _id(sale.get("table_id")) == 2136 and _id(sale.get("contact_id")) == 1265
             and sale.get("customer") == "Walk-In Customer")
    _require(sale.get("status") == "final" and sale.get("is_suspend") == 1 and not sale.get("is_quotation")
             and sale.get("payment_status") == "due" and sale.get("materialize_state") == "draft"
             and not sale.get("payments") and _number(sale.get("amount_paid")) == 0
             and _number(sale.get("payment_revision") or 0) == 0
             and not any(sale.get(field) for field in ("deleted_at", "merged_into", "voided_bill")))
    _require(invoice.get("name") == INVOICE and invoice.get("docstatus") == 0 and invoice.get("status") == "Draft"
             and invoice.get("company") == COMPANY and invoice.get("customer") == "Walk-In Customer"
             and invoice.get("pos_profile") == LOCATION
             and (invoice.get("set_warehouse") in (None, "") if corrected
                  else invoice.get("set_warehouse") == BAR_WAREHOUSE)
             and getdate(invoice.get("posting_date")) == SOLD_AT.date()
             and get_time(invoice.get("posting_time")) == SOLD_AT.time()
             and invoice.get("is_pos") == invoice.get("is_created_using_pos") == invoice.get("update_stock") == 1
             and invoice.get("currency") == "UGX" and _number(invoice.get("conversion_rate")) == 1
             and not invoice.get("payments") and not invoice.get("advances")
             and _number(invoice.get("paid_amount")) == 0 and not invoice.get("amended_from")
             and not invoice.get("is_return") and not any(state["effects"].values()))
    total = NEW_TOTAL if corrected else OLD_TOTAL
    _require(_number(sale.get("final_total")) == total and _number(invoice.get("grand_total")) == total
             and _number(invoice.get("outstanding_amount")) == total)
    for field in ("discount_amount", "tax_amount", "shipping_charges", "packing_charge", "round_off_amount"):
        _require(_number(sale.get(field) or 0) == 0)
    _require(_number(invoice.get("discount_amount") or 0) == 0
             and _number(invoice.get("total_taxes_and_charges") or 0) == 0)
    for index, values in enumerate(RETAINED, 1):
        line_id, uuid, code, product, qty, rate = values
        _check_line(lines[index], line_id=line_id, uuid=uuid, code=code, product=product, qty=qty, rate=rate)
        _check_invoice_row(invoice_rows[index], code, qty, rate)
        _require(not invoice_rows[index].get(MODIFIER_FIELD))
    if not corrected:
        _check_line(lines[0], line_id=OLD_LINE_ID, uuid=OLD_LINE_UUID, code=OLD_ITEM, product=554, qty=1, rate=25000)
        _check_invoice_row(invoice_rows[0], OLD_ITEM, 1, 25000)
        _require(not invoice_rows[0].get(MODIFIER_FIELD) and not sale.get(TOMBSTONES)
                 and setup["new_line_id"] is None and state["work_order"].docstatus == 1)
    else:
        _require(setup["new_line_id"] is not None and setup["new_line_id"] not in {OLD_LINE_ID, *(r[0] for r in RETAINED)})
        _check_line(lines[0], line_id=setup["new_line_id"], uuid=NEW_LINE_UUID, code=NEW_ITEM,
                    product=setup["product_id"], qty=1, rate=20000)
        _check_invoice_row(invoice_rows[0], NEW_ITEM, 1, 30000, kitchen=True)
        _require(lines[0].get("desktop_line_no") == NEW_LINE_NO and not lines[0].get("kot_printed_at")
                 and state["work_order"].docstatus == 2)
        ledger = auto_held_lines._read(sale)
        _require(ledger and ledger["revision"] == 1 and len(ledger["entries"]) == 1)
        entry = ledger["entries"][0]
        _require(entry.get("line_id") == OLD_LINE_ID and entry.get("desktop_line_uuid") == OLD_LINE_UUID
                 and entry.get("removed_by") == "Administrator" and not entry.get("approval"))
        vox_modifiers.validate_invoice_modifiers(invoice)
        options = vox_modifiers.invoice_options(invoice, invoice_rows[0])
        _require(len(options) == 1 and options[0]["public_id"] == setup["modifier_id"]
                 and options[0]["group_name"] == "Sides" and options[0]["label"] == "Chips"
                 and _number(options[0]["price"]) == 10000 and options[0]["stock_policy"] == "sales_only")
    _check_payload(sale, corrected)
    return "already_corrected" if corrected else "ready"


def _state_digest(state):
    return _digest({key: _dict(value) if key in ("sale", "invoice", "work_order") else value
                    for key, value in state.items()})


def _result(state, status):
    return {"state": status, "sale": SALE, "invoice": INVOICE, "invoice_no": LABEL,
            "before_digest": _state_digest(state), "new_line_uuid": NEW_LINE_UUID,
            "new_line_id": state["setup"]["new_line_id"], "replacement_item": NEW_ITEM,
            "base_price": 20000, "side": "Chips", "side_price": 10000, "unchanged_drinks": 24000,
            "approved_total": NEW_TOTAL, "paid_amount": 0, "held": True, "submitted": False,
            "transaction_owner": "caller"}


def preview():
    """Read only: no ID allocation, staging, cancellation, save or enqueue."""
    _guard()
    state = _read_state()
    _access(state["sale"])
    return _result(state, _validate_state(state))


def _new_product(setup):
    return {"desktop_line_uuid": NEW_LINE_UUID, "desktop_line_no": NEW_LINE_NO,
            "product_id": setup["product_id"], "variation_id": setup["product_id"], "quantity": 1,
            "unit_price": 20000, "unit_price_before_discount": 20000, "unit_price_inc_tax": 20000,
            "sub_unit_id": setup["uom_id"], "unit_name": "Pc(s)", "base_unit_multiplier": 1,
            "item_tax": 0, "line_discount_amount": 0, "res_service_staff_id": 2051,
            "kot_printed_at": None, "modifier": [setup["modifier_id"]],
            "modifier_price": [10000], "modifier_quantity": [1]}


def _prepare_removal(sale, setup, access):
    from kit_pos.connector import auto_held_lines, sales, sale_permissions

    payload = _check_payload(sale, False)
    payload["products"] = [_new_product(setup), *deepcopy(payload["products"][1:])]
    payload.update(final_total=NEW_TOTAL, is_suspend=1, payments=[],
                   removed_line_ids=[OLD_LINE_ID], removed_line_uuids=[OLD_LINE_UUID])
    # Existing rows are exact retained quantities, not another application of
    # an old till delta. This is only a normalized incoming validation view.
    for row in payload["products"][1:]:
        row.pop("base_quantity", None)
    sale_permissions.validate_saved_creator(sale, payload)
    sales._authorize_sale_payload(access, payload, sale=sale)
    view = frappe._dict(_dict(sale))
    view[TOMBSTONES] = json.dumps({"schema": 1, "sale_id": SALE, "sale_uuid": SALE_UUID,
        "location_id": LOCATION_ID, "revision": 0, "entries": []}, separators=(",", ":"))
    # Virtual empty ledger exists only on this detached read view. The real
    # sale stays blank until the server's append capability is staged once.
    plan = auto_held_lines.prepare_update(view, payload, access)
    _require(plan.tombstones and not sale.get(TOMBSTONES))
    ledger = _json(plan.tombstones)
    _require(ledger.get("revision") == 1 and len(ledger.get("entries") or []) == 1)
    entry = ledger["entries"][0]
    _require(entry.get("line_id") == OLD_LINE_ID and entry.get("desktop_line_uuid") == OLD_LINE_UUID
             and entry.get("removed_by") == "Administrator" and not entry.get("approval"))
    return plan


@contextmanager
def _no_commit_or_queue():
    commit, enqueue = frappe.db.commit, frappe.enqueue

    def prohibited(*args, **kwargs):
        raise CorrectionError("This private correction must not commit or enqueue; the caller must roll back.")

    frappe.db.commit, frappe.enqueue = prohibited, prohibited
    try:
        yield
    finally:
        frappe.db.commit, frappe.enqueue = commit, enqueue


def _mirror_draft(invoice, sale, location):
    from kit_pos.connector import materialize

    # Same draft operations as _sync_token_draft, without its committing tail.
    _require(invoice.docstatus == 0 and not invoice.get("payments"))
    invoice.customer = materialize._customer_for(sale)
    invoice.is_created_using_pos = 1
    invoice.pos_profile = materialize._pos_profile_for(location)
    for field, value in materialize._posting(sale).items():
        setattr(invoice, field, value)
    invoice.set("items", [])
    for row in materialize._invoice_items(sale, location):
        invoice.append("items", row)
    _require(materialize._bind_exact_opening_validation(invoice, sale), "The original opening must be locked.")
    invoice.save()


def _assert_preserved(before, after):
    sale_before, sale_after = before["sale"], after["sale"]
    _require(_properties(sale_before, {"lines", "payload", TOMBSTONES, "final_total", "modified", "modified_by"})
             == _properties(sale_after, {"lines", "payload", TOMBSTONES, "final_total", "modified", "modified_by"}),
             "An unrelated source-sale property changed; roll back.")
    for old, new in zip(sale_before.get("lines")[1:], sale_after.get("lines")[1:], strict=True):
        _require(_properties(old, {"modified", "modified_by"}) == _properties(new, {"modified", "modified_by"}),
                 "A retained source line changed; roll back.")
    old_payload, new_payload = _json(sale_before.get("payload")), _json(sale_after.get("payload"))
    changed = {"products", "final_total", "removed_line_ids", "removed_line_uuids"}
    _require(_properties(old_payload, changed) == _properties(new_payload, changed),
             "An unrelated original payload header changed; roll back.")
    for old, new in zip(old_payload["products"][1:], new_payload["products"][1:], strict=True):
        # prepare_update may restore an omitted authoritative UUID; no other
        # retained product field is rewritten by this bounded correction.
        _require(_properties(old, {"base_quantity"}) == _properties(new, {"base_quantity"}),
                 "A retained payload product changed; roll back.")
    _require(_properties(before["invoice"], _INVOICE_DERIVED) == _properties(after["invoice"], _INVOICE_DERIVED),
             "An unrelated native invoice header changed; roll back.")
    for old, new in zip(before["invoice"].get("items")[1:], after["invoice"].get("items")[1:], strict=True):
        _require({key: old.get(key) for key in _INVOICE_ITEM_BUSINESS}
                 == {key: new.get(key) for key in _INVOICE_ITEM_BUSINESS},
                 "A retained invoice item's business values changed; roll back.")
    _require(before["setup"]["item"] == after["setup"]["item"]
             and before["setup"]["modifier"] == after["setup"]["modifier"],
             "The approved Item or modifier changed; roll back.")


def apply_correction(*, expected_digest):
    """Apply once inside caller transaction; caller MUST roll back exceptions."""
    from kit_pos.connector import auto_held_lines, item_policy, sales
    from fours_customizations import vox_work_orders

    _guard()
    with _no_commit_or_queue():
        current = _read_state(locked=True)
        access = _access(current["sale"])
        status = _validate_state(current)
        _require(expected_digest == _state_digest(current), "Preview changed; review a fresh exact-bill digest.")
        if status == "already_corrected":
            # Deep native cancellation invariants are checked again, not
            # inferred merely from the new item or the revised total.
            retirement = vox_work_orders._retire_legacy_pilot_work_order()
            _require(retirement["already_cancelled"])
            return _result(current, status)
        before = {key: _dict(value) if key in ("sale", "invoice", "work_order") else deepcopy(value)
                  for key, value in current.items()}
        sale, invoice = current["sale"], current["invoice"]
        plan = _prepare_removal(sale, current["setup"], access)
        retirement = vox_work_orders._retire_legacy_pilot_work_order()
        _require(not retirement["already_cancelled"] and retirement["raw_reservation_released"] == "0.4"
                 and retirement["finished_planned_quantity_released"] == "1")
        auto_held_lines.stage_tombstones(sale, plan.tombstones)
        sales.remove_lines(sale, plan.payload["removed_line_uuids"], plan.payload["removed_line_ids"])
        # Do not replay or rewrite the four retained source lines.
        sales.sync_lines(sale, [plan.payload["products"][0]])
        fresh = [row for row in sale.get("lines") if row.get("desktop_line_uuid") == NEW_LINE_UUID]
        _require(len(fresh) == 1)
        retained = [row for row in sale.get("lines") if row is not fresh[0]]
        fresh[0].idx = 1
        sale.set("lines", [fresh[0], *retained])
        sale.final_total = NEW_TOTAL
        sale.payload = json.dumps(plan.payload, default=str)
        item_policy.validate_sale_items(sale)
        sales.settle_money(sale)
        sale.save()
        _mirror_draft(invoice, sale, current["location"])
        after = _read_state()
        _require(_validate_state(after) == "already_corrected")
        _assert_preserved(before, after)
        result = _result(after, "corrected")
        result.update(previous_digest=expected_digest, retired_work_order=retirement["work_order"],
                      raw_reservation_released="0.4", new_production_created=False)
        return result
