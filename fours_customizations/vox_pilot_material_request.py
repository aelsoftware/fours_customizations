"""Private cancellation of the one explicitly approved unused Vox request.

No method is whitelisted. preview() is read-only; cancel_request() uses native
MR.cancel() and NEVER commits. The caller owns backup, rollback on ANY error,
and the subsequent exact-pilot WO retirement/bill correction. Procurement
history and the original WO link are preserved. No new request is created.
"""

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

import frappe
from frappe.utils import getdate, get_datetime


SITE = "multax.kit.africa"
COMPANY = "Vox Lounge Nansana"
REQUEST = "MAT-MR-2026-00001"
WORK_ORDER = "MFG-WO-2026-00001"
SALE = "4929"
SALE_UUID = "a0812a30-f0c7-443d-803b-3115249811c2"
INVOICE = "SI-2640-198"
WAREHOUSE = "Kitchen - VXN"
RAW = "VXN-RAW-GOAT-KG"
PORTION = "VXN-GOAT-PORTION"
ROWS = (("991eamsbul", RAW, "Kg", Decimal("0.4"), None),
        ("991umca73t", PORTION, "Nos", Decimal(1), "BOM-VXN-GOAT-PORTION-001"))
BIN_FIELDS = ("actual_qty", "stock_value", "valuation_rate", "indented_qty", "ordered_qty",
              "planned_qty", "reserved_qty_for_production", "projected_qty")
_CANCEL_METADATA = {"docstatus", "status", "modified", "modified_by"}


class RequestCancellationError(ValueError):
    pass


def _require(condition, message="The exact approved unused Material Request changed; review it again."):
    if not condition:
        raise RequestCancellationError(message)


def _number(value):
    _require(not isinstance(value, bool) and value not in (None, ""))
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise RequestCancellationError("Invalid quantity in the approved Material Request.") from None
    _require(result.is_finite() and result >= 0)
    return result


def _snapshot(doc):
    return {key: deepcopy(value) for key, value in doc.as_dict().items() if not key.startswith("__")}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _guard():
    _require(frappe.local.site == SITE and frappe.session.user == "Administrator",
             "Only Administrator on Multax may cancel this exact approved request.")
    _require(not getattr(frappe.db, "auto_commit_on_many_writes", False),
             "The caller must retain transaction control.")


def _validate_request(mr):
    _require(mr.doctype == "Material Request" and mr.name == REQUEST and mr.company == COMPANY
             and mr.material_request_type == "Purchase" and mr.work_order == WORK_ORDER
             and (mr.docstatus, mr.status) in ((1, "Pending"), (2, "Cancelled"))
             and mr.owner == "Administrator"
             and get_datetime(mr.creation) == datetime(2026, 9, 29, 10, 34, 52, 918339)
             and getdate(mr.transaction_date) == getdate(mr.schedule_date) == date(2026, 9, 29))
    for field in ("amended_from", "job_card", "customer", "auto_created_via_reorder", "workflow_state",
                  "set_from_warehouse", "set_warehouse", "ignore_linked_doctypes", "transfer_status"):
        _require(not mr.get(field))
    for field in ("per_ordered", "per_received"):
        _require(_number(mr.get(field)) == 0)
    flags = getattr(mr, "flags", None) or {}
    for field in ("ignore_permissions", "ignore_validate", "ignore_links", "ignore_mandatory",
                  "ignore_cancel", "ignore_validate_update_after_submit", "ignore_version"):
        _require(not flags.get(field), "Native permissions, validations and links must not be bypassed.")
    rows = mr.get("items") or []
    _require(len(rows) == len(ROWS))
    for idx, (row, expected) in enumerate(zip(rows, ROWS, strict=True), 1):
        name, code, uom, quantity, bom = expected
        _require(row.name == name and row.get("idx") == idx and row.get("parent") == REQUEST
                 and row.get("parenttype") == "Material Request" and row.get("parentfield") == "items"
                 and row.get("docstatus") == mr.docstatus and row.item_code == code
                 and row.warehouse == WAREHOUSE and not row.get("from_warehouse")
                 and row.uom == row.stock_uom == uom and _number(row.qty) == _number(row.stock_qty) == quantity
                 and _number(row.conversion_factor) == 1 and (row.get("bom_no") or None) == bom
                 and getdate(row.schedule_date) == date(2026, 9, 29))
        for field in ("ordered_qty", "received_qty", "picked_qty", "rate", "price_list_rate", "amount"):
            _require(_number(row.get(field)) == 0)
        for field in ("sales_order", "sales_order_item", "packed_item", "production_plan",
                      "material_request_plan_item", "job_card_item", "project", "wip_composite_asset"):
            _require(not row.get(field))


def _identifier(value):
    """Names come only from native metadata, never from a caller payload."""
    _require(isinstance(value, str) and value and "\x00" not in value)
    return "`" + value.replace("`", "``") + "`"


def _assert_no_dependencies(mr, *, locked):
    from frappe.model.delete_doc import get_linked_docs, get_dynamic_linked_docs
    from frappe.model.dynamic_links import get_dynamic_link_map
    from frappe.model.rename_doc import get_link_fields

    _require(not get_linked_docs(mr, method="Delete") and not get_dynamic_linked_docs(mr, method="Delete"),
             "The request has downstream documents; do not cancel it automatically.")
    suffix = " FOR UPDATE" if locked else ""
    ignored_audit_types = set(frappe.get_hooks("ignore_links_on_delete") or [])
    row_names = tuple(row[0] for row in ROWS)
    # Native backlink helpers use ordinary reads. Repeat their metadata-based
    # checks with current locks when applying, including draft/cancelled
    # operational history and orphan child-row references, not just docstatus1.
    for link in get_link_fields("Material Request"):
        doctype, field = link["parent"], link["fieldname"]
        if doctype in ignored_audit_types:
            continue
        if link["issingle"]:
            rows = frappe.db.sql("SELECT field FROM tabSingles WHERE doctype=%s AND field=%s AND value=%s" + suffix,
                                 (doctype, field, REQUEST))
        else:
            condition, values = f"{_identifier(field)}=%s", [REQUEST]
            if frappe.get_meta(doctype).has_field("material_request_item"):
                condition += " OR material_request_item IN %s"
                values.append(row_names)
            rows = frappe.db.sql(f"SELECT name FROM {_identifier('tab' + doctype)} WHERE {condition}" + suffix,
                                 tuple(values))
        _require(not rows, "The request has downstream documents; do not cancel it automatically.")
    for link in get_dynamic_link_map().get("Material Request", []):
        if link.parent in ignored_audit_types:
            continue
        meta = frappe.get_meta(link.parent)
        if meta.issingle:
            rows = frappe.db.sql("SELECT a.field FROM tabSingles a JOIN tabSingles b ON b.doctype=a.doctype "
                "WHERE a.doctype=%s AND a.field=%s AND a.value=%s AND b.field=%s AND b.value=%s" + suffix,
                (link.parent, link.options, "Material Request", link.fieldname, REQUEST))
        else:
            rows = frappe.db.sql(f"SELECT name FROM {_identifier('tab' + link.parent)} "
                f"WHERE {_identifier(link.options)}=%s AND {_identifier(link.fieldname)}=%s" + suffix,
                ("Material Request", REQUEST))
        _require(not rows, "The request has downstream documents; do not cancel it automatically.")
    for doctype in ("Stock Ledger Entry", "GL Entry", "Stock Reservation Entry"):
        condition = "voucher_no=%s OR voucher_detail_no IN %s"
        values = [REQUEST, row_names]
        if doctype == "GL Entry":
            condition += " OR against_voucher=%s"
            values.append(REQUEST)
        rows = frappe.db.sql(f"SELECT name FROM {_identifier('tab' + doctype)} WHERE {condition}" + suffix,
                             tuple(values))
        _require(not rows, "The request has stock, accounting or reservation history; review it manually.")
    # Do not replace the native final backlink check; additionally run it now,
    # before MaterialRequest.before_cancel changes its status or any Bin hold.
    mr.check_no_back_links_exist()


def _bins(*, locked):
    result = {}
    for _name, code, _uom, _quantity, _bom in ROWS:
        rows = frappe.db.sql("SELECT " + ",".join(BIN_FIELDS) + " FROM tabBin "
            "WHERE item_code=%s AND warehouse=%s" + (" FOR UPDATE" if locked else ""), (code, WAREHOUSE))
        _require(len(rows) == 1, "The approved request's stock bins changed.")
        result[code] = {field: _number(value) for field, value in zip(BIN_FIELDS, rows[0], strict=True)}
    return result


def _read_state(*, locked=False):
    from kit_pos.connector import materialize
    from fours_customizations import vox_work_orders as production

    sale = materialize._locked_sale(SALE) if locked else frappe.get_doc("Kit POS Sale", SALE)
    invoice = frappe.get_doc("Sales Invoice", INVOICE, for_update=locked)
    _require(str(sale.name) == SALE and sale.sale_uuid == SALE_UUID and sale.sales_invoice == INVOICE
             and invoice.name == INVOICE and invoice.company == COMPANY)
    if locked:
        production._lock_inventory()
    work_order = frappe.get_doc("Work Order", WORK_ORDER, for_update=locked)
    mr = frappe.get_doc("Material Request", REQUEST, for_update=locked)
    _validate_request(mr)
    production._validate_pilot_retirement_work_order(work_order,
        state="cancelled" if work_order.docstatus == 2 else "submitted")
    _require(mr.docstatus == 2 or work_order.docstatus == 1)
    masters = []
    for _name, code, uom, _quantity, _bom in ROWS:
        item = frappe.get_doc("Item", code, for_update=locked)
        _require(item.name == code and item.is_stock_item == 1 and item.stock_uom == uom)
        masters.append(_snapshot(item))
    warehouse = frappe.get_doc("Warehouse", WAREHOUSE, for_update=locked)
    _require(warehouse.company == COMPANY and not warehouse.disabled and not warehouse.is_group)
    _assert_no_dependencies(mr, locked=locked)
    bins = _bins(locked=locked)
    if mr.docstatus == 1:
        for _name, code, _uom, quantity, _bom in ROWS:
            _require(bins[code]["indented_qty"] >= quantity)
    surroundings = {"sale": _snapshot(sale), "invoice": _snapshot(invoice), "work_order": _snapshot(work_order),
                    "items": masters, "warehouse": _snapshot(warehouse)}
    fingerprint = {"request": _snapshot(mr), "surroundings": surroundings, "bins": bins}
    return {"request": mr, "surroundings": surroundings, "bins": bins, "digest": _digest(fingerprint)}


def _request_business(mr):
    result = {key: value for key, value in _snapshot(mr).items() if key not in _CANCEL_METADATA}
    result["items"] = [{key: value for key, value in row.items() if key not in _CANCEL_METADATA}
                       for row in result["items"]]
    return result


def preview():
    """Read-only review token; performs no locks, writes or transaction boundary."""
    _guard()
    state = _read_state()
    return {"material_request": REQUEST, "work_order": WORK_ORDER, "digest": state["digest"],
            "already_cancelled": state["request"].docstatus == 2,
            "requested_quantities": {code: str(quantity) for _name, code, _uom, quantity, _bom in ROWS},
            "bins": state["bins"]}


def cancel_request(expected_digest):
    """Cancel ONLY the reviewed unused request; caller must commit or roll back.

    A current pending request must match the preview token exactly. A verified
    already-cancelled original is a no-op, including retry with its old token;
    it never releases demand twice or repairs quantities by direct DB writes.
    """
    _guard()
    _require(isinstance(expected_digest, str) and re.fullmatch(r"[0-9a-f]{64}", expected_digest),
             "A valid reviewed preview digest is required.")
    state = _read_state(locked=True)
    mr = state["request"]
    result = {"material_request": REQUEST, "work_order": WORK_ORDER,
              "already_cancelled": mr.docstatus == 2,
              "released_indented_qty": {RAW: "0", PORTION: "0"},
              "before_digest": state["digest"], "after_digest": state["digest"],
              "stock_movement_created": False}
    if mr.docstatus == 2:
        return result
    _require(state["digest"] == expected_digest, "The request or its stock/dependencies changed; obtain a fresh preview.")
    business_before = _request_business(mr)
    from fours_customizations import vox_work_orders as production

    # Reuse the transaction-boundary guard, not any generated-WO capability.
    # No permissions, native links or validations are bypassed for this MR.
    with production._native_writes():
        mr.cancel()
        after = _read_state(locked=True)
        _require(after["request"].docstatus == 2 and after["request"].status == "Cancelled")
        _require(_request_business(after["request"]) == business_before,
                 "Native cancellation unexpectedly changed the request's business records; roll back.")
        _require(_digest(after["surroundings"]) == _digest(state["surroundings"]),
                 "Native cancellation unexpectedly changed another record; roll back.")
        expected_bins = deepcopy(state["bins"])
        for _name, code, _uom, quantity, _bom in ROWS:
            expected_bins[code]["indented_qty"] -= quantity
            expected_bins[code]["projected_qty"] -= quantity
        _require(after["bins"] == expected_bins,
                 "Native cancellation did not release exactly the approved request quantities; roll back.")
    result.update(after_digest=after["digest"],
                  released_indented_qty={code: str(quantity) for _name, code, _uom, quantity, _bom in ROWS})
    return result
