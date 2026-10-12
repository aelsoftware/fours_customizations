"""Opt-in, native Work Order production for dedicated Vox Nansana dishes.

The caller owns the transaction: these callbacks never commit or enqueue. A
settled, verified Kit POS invoice creates one native WO/Manufacture per stable
sale/line UUID. The native invoice then consumes the finished dish. Cancelling
billing does not uncook food; a payment-only amendment reuses that production.
Side/topping selections are frozen instructions only, never guessed recipes.

Installation and the exact held-order planning helper are explicit operations.
Neither is called at import or by a catalogue/preview. No shared Item is changed.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html import escape
import hashlib
import json
from uuid import UUID

import frappe
from frappe.utils import get_datetime, getdate, get_time, now_datetime

from fours_customizations.vox_kitchen_recipes import (
    BATCHES, COMPONENTS, FINISHED_UOM, LEGACY_MENU, MENU, RAW_UOMS,
)


SITE = "multax.kit.africa"
COMPANY = "Vox Lounge Nansana"
CONFIG = "vox_goat_work_orders"
WAREHOUSE = "Kitchen - VXN"
COST_CENTER = "Kitchen - VXN"
RAW = "VXN-RAW-GOAT-KG"
PORTION = "VXN-GOAT-PORTION"
PER_PORTION = Decimal("0.4")
CUTOVER = datetime(2026, 9, 28, 18, 0, 0)
KEY_FIELD = "custom_vox_production_key"
SNAPSHOT_FIELD = "custom_vox_production_snapshot"
INVOICE_FIELD = "custom_vox_source_invoice"
WORK_ORDER_FIELD = "custom_vox_work_order"
MODIFIER_FIELD = "custom_kit_pos_modifier_reference"
KEY_PREFIX = "VXWO-"
PILOT = {
    "sale": "4929", "sale_uuid": "a0812a30-f0c7-443d-803b-3115249811c2",
    "line_id": "15806", "line_uuid": "28d310b5-8c03-4ce7-9503-8e54e975d81f",
    "invoice": "SI-2640-198", "item_code": "VX-0128",
}
_PILOT_PLANNING_WORK_ORDER = "MFG-WO-2026-00001"
# Canonical original planning-only snapshot, including its original BOM stamp.
# Retirement must never accept a caller-supplied/reconstructed replacement plan.
_PILOT_PLANNING_SNAPSHOT_SHA256 = "9225b8155699f4ed56982e9e3ccf604bc52aca9c33429f0e603866586fdae61d"
_WRITING = ContextVar("vox_native_production_write", default=None)
_RETRYABLE = tuple(error for error in (
    getattr(frappe, "QueryDeadlockError", None), getattr(frappe, "QueryTimeoutError", None),
) if error is not None)

UNVERIFIED = "Kitchen production could not verify this POS order. Keep the bill and ask management to review it."
UNAVAILABLE = "The kitchen Work Order setup is not ready. Keep the bill and ask management to review the kitchen configuration."
SHORTAGE = "Kitchen stock is insufficient to prepare this order. The sale has not been posted; ask management to check kitchen stock."
CHANGED = "This order already has kitchen production with different instructions or quantities. Ask management to review it; it will not be cooked twice."
PROTECTED = "This document belongs to verified kitchen production. Billing cancellation does not reverse prepared food; use a reviewed kitchen adjustment."
VALUATION = "Kitchen stock valuation needs review. The sale has not been posted; ask management to check the kitchen ledger."


class ProductionError(frappe.ValidationError):
    pass


def _require(condition, message=UNVERIFIED):
    if not condition:
        raise ProductionError(message)


@contextmanager
def _public_failure(message):
    """Native details stay in server traceback, not cashier-visible errors."""
    log = getattr(frappe.local, "message_log", None)
    previous = len(log) if isinstance(log, list) else None
    try:
        yield
    except Exception as error:
        current = getattr(frappe.local, "message_log", None)
        if isinstance(current, list):
            del current[previous or 0:]
        if isinstance(error, (ProductionError, *_RETRYABLE)):
            raise
        raise ProductionError(message) from error


def _number(raw, *, zero=False):
    if isinstance(raw, bool) or raw in (None, ""):
        raise ProductionError(UNVERIFIED)
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        raise ProductionError(UNVERIFIED) from None
    _require(value.is_finite() and value >= 0 and (zero or value > 0) and value <= Decimal("1e12"))
    return value


def _decimal_text(value):
    return format(Decimal(str(value)).normalize(), "f")


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _uuid(value):
    try:
        parsed = str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ProductionError(UNVERIFIED) from None
    _require(parsed == str(value).lower())
    return parsed


def production_key(sale_uuid, line_uuid):
    return KEY_PREFIX + _digest([COMPANY, _uuid(sale_uuid), _uuid(line_uuid)])


def _enabled(company):
    if company != COMPANY or getattr(frappe.local, "site", None) != SITE:
        return False
    config = (getattr(frappe, "conf", None) or {}).get(CONFIG) or {}
    _require(isinstance(config, dict), UNAVAILABLE)
    value = config.get(company, False)
    _require(type(value) in (bool, int) and value in (0, 1), UNAVAILABLE)
    return bool(value)


def _scoped_rows(invoice):
    return [row for row in invoice.get("items") or [] if row.get("item_code") in MENU]


def setup_work_order_fields():
    """Explicit Administrator schema installation, outside any business write.

    Custom Field/schema operations perform DDL. Back up first; do not call this
    from an invoice hook, a preview, or a transaction containing pending work.
    """
    _require(frappe.session.user == "Administrator" and frappe.local.site == SITE, UNAVAILABLE)
    _require(not getattr(frappe.db, "transaction_writes", 0), UNAVAILABLE)
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    common = {"read_only": 1, "hidden": 1, "no_copy": 1}
    create_custom_fields({"Work Order": [
        {**common, "fieldname": KEY_FIELD, "label": "Vox Physical Production Key", "fieldtype": "Data",
         "length": 140, "unique": 1, "insert_after": "description"},
        {**common, "fieldname": SNAPSHOT_FIELD, "label": "Vox Frozen Production Instructions",
         "fieldtype": "Long Text", "insert_after": KEY_FIELD},
        {**common, "fieldname": INVOICE_FIELD, "label": "Vox Original Invoice Reference",
         "fieldtype": "Data", "length": 140, "insert_after": SNAPSHOT_FIELD},
    ], "Sales Invoice Item": [
        {**common, "fieldname": KEY_FIELD, "label": "Vox Physical Production Key", "fieldtype": "Data",
         "length": 140, "search_index": 1, "insert_after": "warehouse"},
        {**common, "hidden": 0, "fieldname": WORK_ORDER_FIELD, "label": "Kitchen Work Order",
         "fieldtype": "Link", "options": "Work Order", "insert_after": KEY_FIELD},
    ]}, ignore_validate=False)
    frappe.db.add_unique("Work Order", [KEY_FIELD], constraint_name="vox_physical_production_unique")
    frappe.clear_cache(doctype="Work Order")
    frappe.clear_cache(doctype="Sales Invoice Item")
    _require_schema()
    return {"Work Order": [KEY_FIELD, SNAPSHOT_FIELD, INVOICE_FIELD],
            "Sales Invoice Item": [KEY_FIELD, WORK_ORDER_FIELD]}


def _require_schema():
    _require(all(frappe.db.has_column("Work Order", name)
                 for name in (KEY_FIELD, SNAPSHOT_FIELD, INVOICE_FIELD)), UNAVAILABLE)
    _require(all(frappe.db.has_column("Sales Invoice Item", name)
                 for name in (KEY_FIELD, WORK_ORDER_FIELD)), UNAVAILABLE)
    indexes = frappe.db.sql("SHOW INDEX FROM `tabWork Order` WHERE Non_unique=0", as_dict=True)
    columns = defaultdict(list)
    for row in indexes:
        columns[row.Key_name].append((int(row.Seq_in_index), row.Column_name, row.Sub_part))
    _require(any(sorted(fields) == [(1, KEY_FIELD, None)] for fields in columns.values()), UNAVAILABLE)


def _posted_at(invoice):
    try:
        when = datetime.combine(getdate(invoice.posting_date), get_time(invoice.posting_time))
    except (TypeError, ValueError):
        raise ProductionError(UNVERIFIED) from None
    _require(CUTOVER <= when <= now_datetime(), UNAVAILABLE)
    return when


def route_invoice_warehouses(invoice, method=None):
    """Change only dedicated dish rows, not the invoice/bar header."""
    guard_invoice_trace(invoice, method)
    if not _enabled(invoice.get("company")) or not _scoped_rows(invoice):
        return
    warehouse = frappe.db.get_value("Warehouse", WAREHOUSE, ["company", "disabled", "is_group"], as_dict=True)
    _require(warehouse and warehouse.company == COMPANY and not warehouse.disabled and not warehouse.is_group, UNAVAILABLE)
    for row in _scoped_rows(invoice):
        row.warehouse = WAREHOUSE


def _source_identity(invoice):
    """Only a current native link or the connector's private capability."""
    from kit_pos.connector.materialize import _invoice_submission_context

    context = _invoice_submission_context(invoice)
    linked = frappe.get_all("Kit POS Sale", filters={"sales_invoice": invoice.name},
                            fields=["name"], limit_page_length=2) if invoice.get("name") else []
    _require(len(linked) <= 1)
    if context and linked:
        _require(str(linked[0].name) == str(context.sale.name))
    source = str(context.sale.name) if context else (str(linked[0].name) if linked else None)
    return context, source


def _source_has_menu(invoice):
    """Removing a dedicated row must not remove its source from enforcement."""
    from kit_pos.connector import ids
    from kit_pos.connector.materialize import _locked_sale

    context, source = _source_identity(invoice)
    if not source:
        return False
    sale = _locked_sale(source)
    for line in sale.get("lines") or []:
        if line.get("item_code") in MENU:
            return True
        for fieldname in ("product_id", "variation_id"):
            value = line.get(fieldname)
            if value and ids.lookup("Item", value) in MENU:
                return True
    return False


def _invoice_warehouse_header_matches(invoice, location):
    """Validate row routing before accepting a bar or native mixed-bill default."""
    bar = location.get("warehouse")
    header = invoice.get("set_warehouse")
    rows = invoice.get("items") or []
    if not bar or not rows:
        return False
    if not all(row.get("warehouse") == (WAREHOUSE if row.get("item_code") in MENU else bar)
               for row in rows):
        return False
    if header == bar:
        return True
    if header not in (None, "") or bar == WAREHOUSE:
        return False
    # ERPNext clears the form default only when the rows actually mix warehouses.
    return {row.get("warehouse") for row in rows} == {WAREHOUSE, bar}


def _trusted_source(invoice, *, settled=True):
    from kit_pos.connector.materialize import (
        _locked_sale, _location_for, _pos_profile_for, _customer_for,
        _posting, _reject_finalized_opening,
    )
    context, source = _source_identity(invoice)
    _require(source)
    sale = _locked_sale(source)
    if not context:
        _require(sale.get("sales_invoice") == invoice.name)
    _reject_finalized_opening(sale)
    location = _location_for(sale)
    _require(location and location.get("company") == COMPANY and invoice.company == COMPANY)
    if context:
        _require(str(context.sale.name) == str(sale.name) and context.sale.sale_uuid == sale.sale_uuid)
        _require(all((context.location.get(name) or "") == (location.get(name) or "")
                     for name in ("name", "company", "warehouse", "selling_price_list", "pos_profile")))
    credit = False
    if invoice.get("is_pos") == 0 and invoice.get("is_created_using_pos") == 0:
        # Only the exact owner-approved credit pilot may use the native
        # receivable route. Its private submission capability and durable
        # approval are verified independently of any client payload flags.
        from fours_customizations.vox_pilot_credit import is_exact_approved_credit

        credit = is_exact_approved_credit(invoice, sale, location) is True
    _require(invoice.get("doctype") == "Sales Invoice"
             and ((invoice.get("is_pos") and invoice.get("is_created_using_pos")) or credit)
             and not invoice.get("is_return")
             and invoice.get("update_stock") == 1
             and invoice.get("pos_profile") == _pos_profile_for(location)
             and _invoice_warehouse_header_matches(invoice, location)
             and invoice.get("customer") == _customer_for(sale)
             and invoice.get("currency") == "UGX" and _number(invoice.get("conversion_rate")) == 1)
    if context and sale.get("sales_invoice") and sale.sales_invoice != invoice.name:
        # The only accepted cross-invoice capability is an explicit native
        # amendment of the CURRENT linked cancelled original. No arbitrary
        # duplicate invoice may spend the same prepared portion twice.
        _require(invoice.get("amended_from") == sale.sales_invoice)
        original = frappe.get_doc("Sales Invoice", sale.sales_invoice, for_update=True)
        _require(original.docstatus == 2 and original.company == COMPANY
                 and original.customer == invoice.customer and not original.get("is_return"))
    _require(not any(sale.get(name) for name in ("deleted_at", "voided_bill", "merged_into", "is_quotation")))
    _require(sale.get("status") == "final")
    posting = _posting(sale)
    _require(posting and getdate(invoice.posting_date) == getdate(posting["posting_date"])
             and get_time(invoice.posting_time) == get_time(posting["posting_time"]))
    if settled:
        _require(not sale.get("is_suspend"))
        total = _number(sale.get("final_total"))
        paid = sum((_number(row.get("amount"), zero=True) for row in sale.get("payments") or []
                    if not row.get("is_return")), Decimal(0))
        _require(abs(paid - _number(sale.get("amount_paid"), zero=True)) < Decimal("0.000001")
                 and abs(total - _number(invoice.get("grand_total"))) < 1)
        if credit:
            _require(sale.get("payment_status") == "due" and paid == 0
                     and not sale.get("payments") and not invoice.get("payments")
                     and _number(sale.get("amount_paid"), zero=True) == 0
                     and _number(invoice.get("paid_amount"), zero=True) == 0)
        else:
            _require(sale.get("payment_status") == "paid" and paid > 0 and total - paid < 1)
    _uuid(sale.sale_uuid)
    return sale, location


def _matched_lines(invoice, sale, location):
    """The connector emits exactly one invoice row per source row, in order.

    Validate the entire projection (including unrelated drinks) before using
    that order to recover UUIDs. No matching by a repeated description/price.
    """
    from kit_pos.connector.materialize import _invoice_items

    expected = _invoice_items(sale, location)
    actual = invoice.get("items") or []
    source = sale.get("lines") or []
    _require(len(expected) == len(actual) == len(source))
    result, uuids, identities = [], set(), set()
    for row, projected, line in zip(actual, expected, source, strict=True):
        _require(row.item_code == projected["item_code"] and row.uom == projected["uom"]
                 and (row.get(MODIFIER_FIELD) or "") == (projected.get(MODIFIER_FIELD) or ""))
        for name in ("qty", "rate", "conversion_factor"):
            _require(_number(row.get(name), zero=name == "rate")
                     == _number(projected[name], zero=name == "rate"))
        if row.item_code not in MENU:
            continue
        line_uuid = _uuid(line.get("desktop_line_uuid"))
        identity = str(line.get("line_id") or "")
        _require(identity.isdigit() and int(identity) > 0 and identity not in identities and line_uuid not in uuids)
        identities.add(identity)
        uuids.add(line_uuid)
        _require(row.get("stock_uom") == "Pc(s)" and row.uom == "Pc(s)"
                 and _number(row.conversion_factor) == 1
                 and row.get("warehouse") == WAREHOUSE)
        quantity = _number(row.qty)
        _require(quantity == quantity.to_integral_value() and quantity <= 1000)
        result.append((row, line))
    return result


def _lock_inventory():
    # Every style/bar takes the same ordered raw/finished Item and Bin locks
    # before checking shared chicken, goat and Irish ingredient availability.
    codes = tuple(sorted((*RAW_UOMS, *MENU, PORTION)))
    frappe.db.sql("SELECT name FROM tabItem WHERE name IN %s ORDER BY name FOR UPDATE", (codes,))
    frappe.db.sql("SELECT name FROM tabBin WHERE item_code IN %s AND warehouse=%s "
                  "ORDER BY item_code FOR UPDATE", (codes, WAREHOUSE))
    frappe.db.sql("SELECT name FROM tabBOM WHERE name IN %s ORDER BY name FOR UPDATE", (tuple(MENU.values()),))


def _recipe(code):
    _require(code in MENU, UNAVAILABLE)
    item = frappe.get_doc("Item", code, for_update=True)
    _require(item.is_stock_item == 1 and not item.disabled and item.stock_uom == "Pc(s)"
             and not item.has_serial_no and not item.has_batch_no and not item.has_variants
             and item.is_sales_item == 1 and not item.get("allow_negative_stock")
             and item.get("default_bom") == MENU[code], UNAVAILABLE)
    bom = frappe.get_doc("BOM", MENU[code], for_update=True)
    _require(bom.company == COMPANY and bom.item == code and bom.docstatus == 1 and bom.is_active
             and bom.uom == "Pc(s)" and bom.currency == "UGX" and _number(bom.quantity) == BATCHES[code]
             and not bom.get("with_operations") and not bom.get("operations")
             and not bom.get("secondary_items") and not bom.get("scrap_items")
             and not bom.get("process_loss_percentage") and not bom.get("process_loss_qty")
             and bom.get("transfer_material_against") == "Work Order", UNAVAILABLE)
    rows = bom.get("items") or []
    if code in LEGACY_MENU:
        # This exact schema is signed/frozen in already-posted goat orders.
        # Never migrate or reinterpret those snapshots on a new menu rollout.
        _require(len(rows) == 1, UNAVAILABLE)
        raw = rows[0]
        _require(raw.item_code == RAW and raw.uom == raw.stock_uom == "Kg"
                 and _number(raw.qty) == _number(raw.stock_qty) == 10
                 and _number(raw.conversion_factor) == 1 and raw.source_warehouse == WAREHOUSE
                 and raw.include_item_in_manufacturing == 1 and not raw.get("bom_no"), UNAVAILABLE)
        return {"bom": bom.name, "modified": str(bom.modified), "quantity": "25", "uom": "Pc(s)",
                "raw_item": RAW, "raw_qty": "10", "raw_uom": "Kg", "raw_per_portion": "0.4",
                "warehouse": WAREHOUSE}
    components = COMPONENTS[code]
    _require(item.get("custom_company") in (None, "", COMPANY), UNAVAILABLE)
    by_code = {row.get("item_code"): row for row in rows}
    _require(len(rows) == len(by_code) == len(components)
             and set(by_code) == {raw_code for raw_code, _uom, _per_plate in components}, UNAVAILABLE)
    ingredients = []
    for raw_code, uom, per_plate in components:
        raw = by_code[raw_code]
        quantity = BATCHES[code] * per_plate
        _require(raw.uom == raw.stock_uom == uom
                 and _number(raw.qty) == _number(raw.stock_qty) == quantity
                 and _number(raw.conversion_factor) == 1 and raw.source_warehouse == WAREHOUSE
                 and raw.include_item_in_manufacturing == 1 and not raw.get("bom_no"), UNAVAILABLE)
        raw_item = frappe.get_doc("Item", raw_code, for_update=True)
        _require(raw_item.is_stock_item == 1 and not raw_item.disabled and raw_item.stock_uom == uom
                 and not raw_item.is_sales_item and not raw_item.has_serial_no and not raw_item.has_batch_no
                 and not raw_item.has_variants and not raw_item.get("allow_negative_stock")
                 and raw_item.get("custom_company") in (None, "", COMPANY), UNAVAILABLE)
        unit = frappe.get_doc("UOM", uom, for_update=True)
        _require(unit.name == uom and unit.get("enabled") == 1
                 and unit.get("must_be_whole_number") == 0, UNAVAILABLE)
        ingredients.append({"item_code": raw_code, "quantity": _decimal_text(quantity), "uom": uom,
                            "per_portion": _decimal_text(per_plate)})
    return {"bom": bom.name, "modified": str(bom.modified), "quantity": _decimal_text(BATCHES[code]),
            "uom": FINISHED_UOM, "ingredients": ingredients, "warehouse": WAREHOUSE}


def _plan(invoice, sale, row, line):
    from fours_customizations.vox_modifiers import invoice_options

    try:
        options = invoice_options(invoice, row)
    except (ValueError, TypeError):
        raise ProductionError(UNVERIFIED) from None
    _require(isinstance(options, list) and len(options) <= 64)
    for option in options:
        _require(isinstance(option, dict) and option.get("company") == COMPANY
                 and option.get("item_code") == row.item_code and option.get("stock_policy") == "sales_only"
                 and option.get("selection") == row.get(MODIFIER_FIELD) and option.get("label"))
    quantity = _number(row.qty)
    plan = {"version": 1, "company": COMPANY, "sale": str(sale.name),
            "sale_label": str(sale.get("invoice_no") or "")[:100], "sale_uuid": _uuid(sale.sale_uuid),
            "line_id": str(line.line_id), "line_uuid": _uuid(line.desktop_line_uuid),
            "source_item": row.item_code, "production_item": row.item_code,
            "quantity": _decimal_text(quantity), "uom": "Pc(s)", "raw_quantity": _decimal_text(quantity * PER_PORTION),
            "posting_datetime": str(_posted_at(invoice)), "recipe": _recipe(row.item_code),
            "modifier_reference": row.get(MODIFIER_FIELD) or "",
            "instructions": sorted(options, key=lambda option: int(option["public_id"])), "planning_only": False}
    if row.item_code not in LEGACY_MENU:
        plan["version"] = 2
        del plan["raw_quantity"]
        plan["ingredients"] = [
            {"item_code": code, "quantity": _decimal_text(quantity * per_plate),
             "uom": uom, "warehouse": WAREHOUSE}
            for code, uom, per_plate in COMPONENTS[row.item_code]
        ]
    return plan


def _ingredients(plan):
    """Read only a recognized frozen schema; never infer a new raw material.

    Legacy goat snapshots remain byte-for-byte schema 1. The new dishes use
    schema 2 so one quantity can never be confused with another ingredient.
    """
    code = plan.get("production_item")
    quantity = _number(plan.get("quantity"))
    _require(type(plan.get("version")) is int, CHANGED)
    if plan["version"] == 1:
        _require(code in LEGACY_MENU or code == PORTION, CHANGED)
        raw_quantity = _number(plan.get("raw_quantity"))
        _require(raw_quantity == quantity * PER_PORTION and "ingredients" not in plan, CHANGED)
        return [{"item_code": RAW, "quantity": raw_quantity, "uom": "Kg", "warehouse": WAREHOUSE}]
    _require(plan["version"] == 2 and code in MENU and code not in LEGACY_MENU
             and "raw_quantity" not in plan and not plan.get("planning_only")
             and plan.get("uom") == FINISHED_UOM, CHANGED)
    expected = [
        {"item_code": raw_code, "quantity": _decimal_text(quantity * per_plate),
         "uom": uom, "warehouse": WAREHOUSE}
        for raw_code, uom, per_plate in COMPONENTS[code]
    ]
    _require(plan.get("ingredients") == expected, CHANGED)
    return [{**component, "quantity": _number(component["quantity"])} for component in expected]


def _existing(plan):
    key = production_key(plan["sale_uuid"], plan["line_uuid"])
    rows = frappe.db.sql(f"SELECT name FROM `tabWork Order` WHERE `{KEY_FIELD}`=%s FOR UPDATE", (key,), as_dict=True)
    _require(len(rows) <= 1, CHANGED)
    return frappe.get_doc("Work Order", rows[0].name, for_update=True) if rows else None


def _validate_work_order(wo, plan, *, completed):
    _require(wo.get(KEY_FIELD) == production_key(plan["sale_uuid"], plan["line_uuid"]), CHANGED)
    try:
        frozen = json.loads(wo.get(SNAPSHOT_FIELD) or "")
    except (TypeError, ValueError):
        raise ProductionError(CHANGED) from None
    # BOM valuation refreshes may update modified without changing a recipe.
    # Keep the original timestamp as audit evidence, compare actual semantics.
    def semantics(value):
        if not isinstance(value, dict) or not isinstance(value.get("recipe"), dict):
            return None
        return {**value, "recipe": {key: part for key, part in value["recipe"].items() if key != "modified"}}

    _require(semantics(frozen) == semantics(plan) and wo.company == COMPANY and wo.production_item == plan["production_item"]
             and wo.bom_no == plan["recipe"]["bom"] and wo.docstatus == 1
             and _number(wo.qty) == _number(plan["quantity"]) and wo.stock_uom == plan["uom"]
             and wo.source_warehouse == wo.fg_warehouse == WAREHOUSE
             and wo.skip_transfer == 1 and not wo.from_wip_warehouse and not wo.wip_warehouse
             and not wo.operations and not wo.get("process_loss_qty") and not wo.get("disassembled_qty"), CHANGED)
    ingredients = _ingredients(plan)
    rows = wo.get("required_items") or []
    by_code = {row.get("item_code"): row for row in rows}
    _require(len(rows) == len(by_code) == len(ingredients)
             and set(by_code) == {raw["item_code"] for raw in ingredients}, CHANGED)
    for raw in ingredients:
        row = by_code[raw["item_code"]]
        _require(row.stock_uom == raw["uom"] and row.source_warehouse == raw["warehouse"]
                 and _number(row.required_qty) == raw["quantity"], CHANGED)
        if plan["version"] == 2:
            _require(row.get("include_item_in_manufacturing") == 1
                     and not any(row.get(name) for name in (
                         "operation", "operation_row_id", "allow_alternative_item", "is_additional_item",
                         "is_customer_provided_item", "voucher_detail_reference")), CHANGED)
        if completed:
            _require(_number(row.consumed_qty) == raw["quantity"], CHANGED)
        else:
            _require(not row.consumed_qty, CHANGED)
    if completed:
        _require(wo.status == "Completed" and _number(wo.produced_qty) == _number(plan["quantity"]), CHANGED)
    else:
        _require(wo.status in ("Not Started", "Stock Reserved", "Stock Partially Reserved")
                 and not wo.produced_qty, CHANGED)


def _manufacture_for(wo, plan):
    rows = frappe.db.sql("SELECT name,purpose,docstatus FROM `tabStock Entry` WHERE work_order=%s FOR UPDATE",
                         (wo.name,), as_dict=True)
    _require(len(rows) == 1 and rows[0].purpose == "Manufacture" and rows[0].docstatus == 1, CHANGED)
    entry = frappe.get_doc("Stock Entry", rows[0].name, for_update=True)
    entry_when = datetime.combine(getdate(entry.posting_date), get_time(entry.posting_time))
    _require(entry.company == COMPANY and entry.work_order == wo.name and entry.bom_no == plan["recipe"]["bom"]
             and entry_when == get_datetime(plan["posting_datetime"])
             and _number(entry.fg_completed_qty) == _number(plan["quantity"])
             and not entry.get("process_loss_qty") and not entry.get("additional_costs")
             and not entry.get("is_return") and not entry.get("job_card"), CHANGED)
    ingredients = _ingredients(plan)
    raw_codes = {raw["item_code"] for raw in ingredients}
    raw_rows = [row for row in entry.items if row.item_code in raw_codes
                and row.s_warehouse == WAREHOUSE and not row.t_warehouse and not row.is_finished_item]
    by_code = {row.get("item_code"): row for row in raw_rows}
    finished = [row for row in entry.items if row.item_code == plan["production_item"]
                and row.t_warehouse == WAREHOUSE and not row.s_warehouse and row.is_finished_item]
    _require(len(entry.items) == len(ingredients) + 1
             and len(raw_rows) == len(by_code) == len(ingredients)
             and set(by_code) == raw_codes and len(finished) == 1
             and _number(finished[0].transfer_qty) == _number(plan["quantity"]), CHANGED)
    for raw in ingredients:
        row = by_code[raw["item_code"]]
        _require(_number(row.transfer_qty) == raw["quantity"], CHANGED)
        if plan["version"] == 2:
            _require(row.get("stock_uom") == row.get("uom") == raw["uom"]
                     and _number(row.get("conversion_factor")) == 1
                     and _number(row.get("qty")) == raw["quantity"], CHANGED)
    if plan["version"] == 2:
        _require(finished[0].get("stock_uom") == finished[0].get("uom") == FINISHED_UOM
                 and _number(finished[0].get("conversion_factor")) == 1
                 and _number(finished[0].get("qty")) == _number(plan["quantity"]), CHANGED)
    return entry


def _stock_at(code, when):
    previous = frappe.db.sql("SELECT qty_after_transaction FROM `tabStock Ledger Entry` "
        "WHERE item_code=%s AND warehouse=%s AND is_cancelled=0 AND posting_datetime<=%s "
        "ORDER BY posting_datetime DESC,creation DESC,name DESC LIMIT 1 FOR UPDATE", (code, WAREHOUSE, when))
    return Decimal(str(previous[0][0])) if previous else Decimal(0)


def _actual(code):
    values = frappe.db.sql("SELECT actual_qty FROM tabBin WHERE item_code=%s AND warehouse=%s FOR UPDATE",
                           (code, WAREHOUSE))
    _require(len(values) <= 1, UNAVAILABLE)
    value = Decimal(str(values[0][0])) if values else Decimal(0)
    _require(value.is_finite(), UNAVAILABLE)
    return value


def _available(code, when, *, future_floor=False, exclude_work_order=None):
    from fours_customizations.vox_catalogue_stock import reservation_hold

    # Every quantity is a current locking read. A plain SELECT after acquiring
    # a Bin lock would still read an old REPEATABLE-READ transaction snapshot.
    available = min(_actual(code), _stock_at(code, when))
    if future_floor:
        later = frappe.db.sql("SELECT qty_after_transaction FROM `tabStock Ledger Entry` "
            "WHERE item_code=%s AND warehouse=%s AND is_cancelled=0 AND posting_datetime>%s "
            "ORDER BY posting_datetime,creation,name FOR UPDATE", (code, WAREHOUSE, when))
        for row in later:
            available = min(available, Decimal(str(row[0])))
    try:
        reserved = reservation_hold(code, WAREHOUSE, exclude_work_order=exclude_work_order, for_update=True)
    except ValueError:
        raise ProductionError(UNAVAILABLE) from None
    _require(available.is_finite() and reserved.is_finite() and reserved >= 0, UNAVAILABLE)
    return available - reserved


@dataclass
class _WriteContext:
    user: str
    site: str
    database: object
    objects: dict[int, object] = field(default_factory=dict)
    retiring_pilot: object | None = None


def _database():
    getter = getattr(frappe.db, "_get_current_object", None)
    return getter() if callable(getter) else frappe.db


@contextmanager
def _native_writes():
    _require(not getattr(frappe.db, "auto_commit_on_many_writes", False), UNAVAILABLE)
    # Frappe increments _disable_transaction_control for EVERY app hook and
    # may nest it further while WO/Manufacture hooks run. A positive counter
    # is expected protection, not an unsafe transaction. Never reset/change
    # that framework-owned counter; additionally fail any attempted commit
    # below instead of Frappe's normal warning-and-no-op behavior in hooks.
    context = _WriteContext(frappe.session.user, frappe.local.site, _database())
    token = _WRITING.set(context)
    original_commit = frappe.db.commit
    def prohibited(*args, **kwargs):
        raise ProductionError("Kitchen production attempted an unsafe transaction boundary; the sale must be retried.")
    frappe.db.commit = prohibited
    try:
        yield context
    finally:
        frappe.db.commit = original_commit
        _WRITING.reset(token)


def _new_work_order(invoice, plan, context):
    _require(_WRITING.get() is context, PROTECTED)
    wo = frappe.get_doc({"doctype": "Work Order", "naming_series": "MFG-WO-.YYYY.-", "company": COMPANY,
        "production_item": plan["production_item"], "bom_no": plan["recipe"]["bom"],
        "qty": float(_number(plan["quantity"])), "stock_uom": plan["uom"],
        "source_warehouse": WAREHOUSE, "fg_warehouse": WAREHOUSE,
        "skip_transfer": 1, "from_wip_warehouse": 0, "reserve_stock": 0, "use_multi_level_bom": 0,
        "transfer_material_against": "Work Order", "planned_start_date": plan["posting_datetime"],
        KEY_FIELD: production_key(plan["sale_uuid"], plan["line_uuid"]),
        SNAPSHOT_FIELD: _canonical(plan), INVOICE_FIELD: invoice.name,
        "description": "Kit POS " + escape(plan.get("sale_label") or plan["sale"]) + " (source "
            + plan["sale"] + ") / line " + plan["line_id"] + ": "
            + ("Planning instruction only; not yet prepared. " if plan["planning_only"] else "Prepare the ordered dish. ")
            + "; ".join(escape(str(option["label"])[:160]) for option in plan["instructions"])
            + ". Side/topping choices are instructions only; their ingredient portions are not configured."})
    context.objects[id(wo)] = wo
    wo.get_items_and_operations_from_bom()
    # This single generated object is authorized by the verified settled sale;
    # no cashier roles, session/global flags or validation bypasses are added.
    wo.insert(ignore_permissions=True)
    wo.submit()
    _validate_work_order(frappe.get_doc("Work Order", wo.name, for_update=True), plan, completed=False)
    return wo


def _complete_work_order(wo, plan, context):
    from erpnext.manufacturing.doctype.work_order.work_order import make_stock_entry

    _require(_WRITING.get() is context, PROTECTED)
    when = get_datetime(plan["posting_datetime"])
    for raw in _ingredients(plan):
        _require(_available(raw["item_code"], when, future_floor=True, exclude_work_order=wo.name)
                 >= raw["quantity"], SHORTAGE)
    values = make_stock_entry(wo.name, "Manufacture", qty=float(_number(plan["quantity"])))
    values.update({"posting_date": when.date(), "posting_time": when.time(), "set_posting_time": 1,
                   "remarks": "Native kitchen production " + wo.get(KEY_FIELD) + "; stock is issued by the Sales Invoice."})
    entry = frappe.get_doc(values)
    context.objects[id(entry)] = entry
    entry.insert(ignore_permissions=True)
    entry.submit()
    current = frappe.get_doc("Work Order", wo.name, for_update=True)
    _validate_work_order(current, plan, completed=True)
    _manufacture_for(current, plan)
    return current


def _assert_lineage(invoice, row, plan, wo):
    """One stable physical key can fund only one live native invoice row."""
    key = production_key(plan["sale_uuid"], plan["line_uuid"])
    active = frappe.db.sql(f"SELECT item.parent FROM `tabSales Invoice Item` item "
        "JOIN `tabSales Invoice` invoice ON invoice.name=item.parent "
        f"WHERE item.`{KEY_FIELD}`=%s AND invoice.docstatus=1 AND invoice.name!=%s FOR UPDATE",
        (key, invoice.name))
    _require(not active, CHANGED)
    _require(not row.get(KEY_FIELD) or row.get(KEY_FIELD) == key, CHANGED)
    if wo is None:
        _require(not row.get(WORK_ORDER_FIELD) and not invoice.get("amended_from"), CHANGED)
        return
    _require(not row.get(WORK_ORDER_FIELD) or row.get(WORK_ORDER_FIELD) == wo.name, CHANGED)
    if invoice.name == wo.get(INVOICE_FIELD):
        return
    origin = invoice.get("amended_from")
    _require(origin and origin != invoice.name, CHANGED)
    original = frappe.get_doc("Sales Invoice", origin, for_update=True)
    _require(original.docstatus == 2 and original.company == COMPANY and not original.get("is_return"), CHANGED)
    ancestors = [candidate for candidate in original.items if candidate.get(KEY_FIELD) == key]
    _require(len(ancestors) == 1, CHANGED)
    ancestor = ancestors[0]
    _require(ancestor.get(WORK_ORDER_FIELD) == wo.name, CHANGED)
    for name in ("item_code", "uom", "stock_uom", "warehouse", MODIFIER_FIELD):
        _require((ancestor.get(name) or "") == (row.get(name) or ""), CHANGED)
    for name in ("qty", "rate", "conversion_factor"):
        _require(_number(ancestor.get(name), zero=name == "rate")
                 == _number(row.get(name), zero=name == "rate"), CHANGED)


def _ensure_invoice_production(invoice):
    _require_schema()
    _require(not invoice.get("is_return") and invoice.get("update_stock") == 1)
    _require(not frappe.db.get_value("Company", COMPANY, "enable_selling_automations"), UNAVAILABLE)
    _require(not frappe.db.get_single_value("Stock Settings", "auto_reserve_stock"), UNAVAILABLE)
    sale, location = _trusted_source(invoice)
    when = _posted_at(invoice)
    _lock_inventory()
    pairs = _matched_lines(invoice, sale, location)
    plans = [_plan(invoice, sale, row, line) for row, line in pairs]
    _require(len(plans) == len(_scoped_rows(invoice)) and plans)
    old = [_existing(plan) for plan in plans]
    for (row, _line), plan, wo in zip(pairs, plans, old, strict=True):
        _assert_lineage(invoice, row, plan, wo)
        if wo:
            _validate_work_order(wo, plan, completed=True)
            _manufacture_for(wo, plan)
    # These SKUs are deliberately not batched. Do not let native valuation
    # spend another cancelled order's fungible finished dishes. Check once
    # before cooking ANY line so two new same-style lines can cook together.
    for code in {plan["production_item"] for plan in plans}:
        matches = [(plan, wo) for plan, wo in zip(plans, old, strict=True) if plan["production_item"] == code]
        has_new = any(wo is None for _plan_value, wo in matches)
        existing_qty = sum((_number(value["quantity"]) for value, wo in matches if wo is not None), Decimal(0))
        _require(not (has_new and existing_qty), CHANGED)
        _require(_actual(code) == (Decimal(0) if has_new else existing_qty), CHANGED)
    new_raw = defaultdict(Decimal)
    for plan, wo in zip(plans, old, strict=True):
        if wo is None:
            for raw in _ingredients(plan):
                new_raw[raw["item_code"]] += raw["quantity"]
    # Aggregate the entire cart before producing any line. Chicken/Bowls share
    # bird stock, Goat/Bowls share goat, and both Bowls share Irish potatoes.
    for code, needed in sorted(new_raw.items()):
        _require(_available(code, when, future_floor=True) >= needed, SHORTAGE)
    global_negative = frappe.db.get_single_value("Stock Settings", "allow_negative_stock")
    result = []
    with _native_writes() as context:
        made = []
        for (row, _line), plan, wo in zip(pairs, plans, old, strict=True):
            if wo is None:
                wo = _new_work_order(invoice, plan, context)
                wo = _complete_work_order(wo, plan, context)
                made.append((plan, wo))
            setattr(row, KEY_FIELD, production_key(plan["sale_uuid"], plan["line_uuid"]))
            setattr(row, WORK_ORDER_FIELD, wo.name)
            result.append(wo.name)
        demand = defaultdict(Decimal)
        for plan in plans:
            demand[plan["production_item"]] += _number(plan["quantity"])
        for code, needed in demand.items():
            _require(_available(code, when) >= needed, SHORTAGE)
        if any(plan["version"] == 2 for plan, _wo in made):
            # Only newly cooked food participates. A billing amendment reuses
            # its original food and must never re-consume/restore raw stock.
            _settle_raw_bin_values(made, context)
        _require(frappe.db.get_single_value("Stock Settings", "allow_negative_stock") == global_negative, UNAVAILABLE)
    return result


def ensure_invoice_production(invoice, method=None):
    guard_invoice_trace(invoice, method)
    if not _enabled(invoice.get("company")):
        return
    with _public_failure(UNVERIFIED):
        current = _scoped_rows(invoice)
        traces = [row for row in invoice.get("items") or [] if row.get(KEY_FIELD) or row.get(WORK_ORDER_FIELD)]
        if not current and not traces and not _source_has_menu(invoice):
            return
        _require(all(row.item_code in MENU for row in traces), CHANGED)
        with _public_failure(UNAVAILABLE):
            return _ensure_invoice_production(invoice)


def _valuation_snapshot(codes, when):
    """Current, bounded evidence for the native finished-item replay only."""
    sles = frappe.db.sql("SELECT * FROM `tabStock Ledger Entry` WHERE item_code IN %s "
        "AND warehouse=%s AND is_cancelled=0 AND posting_datetime>=%s "
        "ORDER BY posting_datetime,creation,name LIMIT 2001 FOR UPDATE", (codes, WAREHOUSE, when), as_dict=True)
    _require(sles and len(sles) <= 2000, VALUATION)
    _require(all(row.voucher_type in ("Stock Entry", "Sales Invoice") for row in sles), VALUATION)
    previous = []
    for code in codes:
        baseline = frappe.db.sql("SELECT * FROM `tabStock Ledger Entry` WHERE item_code=%s "
            "AND warehouse=%s AND is_cancelled=0 AND posting_datetime<%s "
            "ORDER BY posting_datetime DESC,creation DESC LIMIT 2 FOR UPDATE", (code, WAREHOUSE, when), as_dict=True)
        _require(len(baseline) < 2 or (baseline[0].posting_datetime, baseline[0].creation)
                 != (baseline[1].posting_datetime, baseline[1].creation), VALUATION)
        previous.extend(baseline[:1])
    references = tuple(sorted({(row.voucher_type, row.voucher_no) for row in sles}))
    vouchers = {}
    for doctype, name in references:
        doc = frappe.get_doc(doctype, name, for_update=True)
        _require(doc.docstatus == 1 and doc.company == COMPANY, VALUATION)
        vouchers[(doctype, name)] = doc.as_dict()
    gl = frappe.db.sql("SELECT * FROM `tabGL Entry` WHERE (voucher_type,voucher_no) IN %s "
                       "ORDER BY voucher_type,voucher_no,name FOR UPDATE", (references,), as_dict=True)
    bins = frappe.db.sql("SELECT * FROM tabBin WHERE item_code IN %s AND warehouse=%s "
        "ORDER BY item_code FOR UPDATE", (tuple(sorted((*RAW_UOMS, *MENU, PORTION))), WAREHOUSE), as_dict=True)
    return {"sles": sles, "previous": previous, "bins": bins, "vouchers": vouchers, "gl": gl}


def _raw_valuation_snapshot(codes, when):
    """Lock a bounded raw timeline and every referenced physical/accounting row.

    Future native receipts are valid evidence too, but no coordinated repost
    is performed here. An inconsistent future value/quantity chain or a
    quantity-reset reconciliation requiring reconstruction aborts.
    """
    sles = frappe.db.sql("SELECT * FROM `tabStock Ledger Entry` WHERE item_code IN %s "
        "AND warehouse=%s AND is_cancelled=0 AND posting_datetime>=%s "
        "ORDER BY posting_datetime,creation,name LIMIT 2001 FOR UPDATE", (codes, WAREHOUSE, when), as_dict=True)
    _require(sles and len(sles) <= 2000, VALUATION)
    allowed = ("Stock Entry", "Purchase Receipt", "Purchase Invoice", "Stock Reconciliation")
    _require(all(row.voucher_type in allowed for row in sles), VALUATION)
    previous = []
    for code in codes:
        baseline = frappe.db.sql("SELECT * FROM `tabStock Ledger Entry` WHERE item_code=%s "
            "AND warehouse=%s AND is_cancelled=0 AND posting_datetime<%s "
            "ORDER BY posting_datetime DESC,creation DESC LIMIT 2 FOR UPDATE", (code, WAREHOUSE, when), as_dict=True)
        _require(len(baseline) < 2 or (baseline[0].posting_datetime, baseline[0].creation)
                 != (baseline[1].posting_datetime, baseline[1].creation), VALUATION)
        previous.extend(baseline[:1])
    references = tuple(sorted({(row.voucher_type, row.voucher_no) for row in sles}))
    vouchers, work_orders = {}, {}
    for doctype, name in references:
        doc = frappe.get_doc(doctype, name, for_update=True)
        _require(doc.docstatus == 1 and doc.company == COMPANY, VALUATION)
        vouchers[(doctype, name)] = doc.as_dict()
        if doctype == "Stock Entry" and doc.get("purpose") == "Manufacture":
            _require(doc.get("work_order"), VALUATION)
            wo = frappe.get_doc("Work Order", doc.work_order, for_update=True)
            _require(wo.production_item in MENU and wo.bom_no == MENU[wo.production_item], VALUATION)
            try:
                plan = json.loads(wo.get(SNAPSHOT_FIELD) or "")
            except (ValueError, TypeError):
                raise ProductionError(VALUATION) from None
            _validate_work_order(wo, plan, completed=True)
            _require(_manufacture_for(wo, plan).name == doc.name, VALUATION)
            work_orders[wo.name] = wo.as_dict()
    voucher_sles = frappe.db.sql("SELECT * FROM `tabStock Ledger Entry` "
        "WHERE (voucher_type,voucher_no) IN %s ORDER BY voucher_type,voucher_no,creation,name "
        "LIMIT 2001 FOR UPDATE", (references,), as_dict=True)
    _require(voucher_sles and len(voucher_sles) <= 2000, VALUATION)
    gl = frappe.db.sql("SELECT * FROM `tabGL Entry` WHERE (voucher_type,voucher_no) IN %s "
        "ORDER BY voucher_type,voucher_no,name LIMIT 4001 FOR UPDATE", (references,), as_dict=True)
    _require(len(gl) <= 4000, VALUATION)
    bins = frappe.db.sql("SELECT * FROM tabBin WHERE item_code IN %s AND warehouse=%s "
        "ORDER BY item_code FOR UPDATE", (tuple(sorted((*RAW_UOMS, *MENU, PORTION))), WAREHOUSE), as_dict=True)
    return {"sles": sles, "previous": previous, "voucher_sles": voucher_sles,
            "vouchers": vouchers, "work_orders": work_orders, "gl": gl, "bins": bins}


def _raw_valuation_balances(state, codes):
    """Validate authoritative balances, including native Manufacture links.

    Unlike a raw-item repost, the cache update never follows a dependency.
    Nevertheless every linked raw issue must point to the exact finished row
    of a completed protected native WO, not an arbitrary cross-item voucher.
    """
    _require(state["sles"] and len(state["sles"]) <= 2000 and set(codes) <= set(RAW_UOMS), VALUATION)
    precisions = _raw_valuation_precisions()
    balances, values, latest, seen = {}, {}, {}, set()
    for row in state["previous"]:
        _require(row.item_code in codes and row.item_code not in balances and row.company == COMPANY
                 and row.warehouse == WAREHOUSE and not row.is_cancelled
                 and row.stock_uom == RAW_UOMS[row.item_code], VALUATION)
        balances[row.item_code] = _number(row.qty_after_transaction, zero=True)
        values[row.item_code] = _number(row.stock_value, zero=True)
        _number(row.valuation_rate, zero=True)
        _require(balances[row.item_code] != 0 or values[row.item_code] == 0, VALUATION)
        _raw_valuation_rate_matches(row, precisions)
    for code in codes:
        balances.setdefault(code, Decimal(0))
        values.setdefault(code, Decimal(0))
    ordering = [(get_datetime(row.posting_datetime), get_datetime(row.creation), row.name) for row in state["sles"]]
    _require(ordering == sorted(ordering), VALUATION)
    for row in state["sles"]:
        identity = (row.item_code, get_datetime(row.posting_datetime), get_datetime(row.creation))
        _require(row.item_code in codes and row.company == COMPANY and row.warehouse == WAREHOUSE
                 and row.stock_uom == RAW_UOMS[row.item_code] and not row.is_cancelled and identity not in seen
                 and not any(row.get(name) for name in (
                     "serial_no", "batch_no", "serial_and_batch_bundle", "has_serial_no", "has_batch_no",
                     "is_adjustment_entry")), VALUATION)
        seen.add(identity)
        voucher = state["vouchers"].get((row.voucher_type, row.voucher_no))
        _require(voucher and voucher.get("docstatus") == 1 and voucher.get("company") == COMPANY, VALUATION)
        detail = [part for part in voucher.get("items") or [] if part.get("name") == row.voucher_detail_no]
        _require(len(detail) == 1 and detail[0].get("item_code") == row.item_code, VALUATION)
        dependency = row.get("dependant_sle_voucher_detail_no")
        if row.voucher_type == "Stock Entry" and voucher.get("purpose") == "Manufacture":
            wo = state["work_orders"].get(voucher.get("work_order"))
            finished = [part for part in voucher.get("items") or [] if part.get("is_finished_item")]
            _require(wo and wo.get("docstatus") == 1 and wo.get("status") == "Completed"
                     and len(finished) == 1 and dependency == finished[0].get("name")
                     and finished[0].get("item_code") == wo.get("production_item")
                     and finished[0].get("t_warehouse") == WAREHOUSE and not finished[0].get("s_warehouse")
                     and detail[0].get("s_warehouse") == WAREHOUSE and not detail[0].get("t_warehouse")
                     and not detail[0].get("is_finished_item"), VALUATION)
            targets = [sle for sle in state["voucher_sles"] if sle.voucher_type == "Stock Entry"
                       and sle.voucher_no == row.voucher_no and sle.voucher_detail_no == dependency]
            _require(len(targets) == 1 and targets[0].item_code == wo.get("production_item")
                     and targets[0].company == COMPANY and targets[0].warehouse == WAREHOUSE
                     and not targets[0].is_cancelled
                     and _number(targets[0].actual_qty) == _number(voucher.get("fg_completed_qty")), VALUATION)
            _require(Decimal(str(row.actual_qty)) == -_number(detail[0].get("transfer_qty"))
                     and Decimal(str(row.stock_value_difference)) == -_number(detail[0].get("basic_amount"), zero=True),
                     VALUATION)
        else:
            _require(not dependency and row.voucher_type in (
                "Stock Entry", "Purchase Receipt", "Purchase Invoice", "Stock Reconciliation"), VALUATION)
        delta = Decimal(str(row.actual_qty))
        difference = Decimal(str(row.stock_value_difference))
        _require(delta.is_finite() and abs(delta) <= Decimal("1e12") and difference.is_finite(), VALUATION)
        balances[row.item_code] += delta
        _require(balances[row.item_code] == _number(row.qty_after_transaction, zero=True), VALUATION)
        value = _number(row.stock_value, zero=True)
        _require(value - values[row.item_code] == difference, VALUATION)
        values[row.item_code] = value
        _number(row.valuation_rate, zero=True)
        _require(balances[row.item_code] != 0 or value == 0, VALUATION)
        _raw_valuation_rate_matches(row, precisions)
        latest[row.item_code] = row
    _require(set(latest) == set(codes), VALUATION)
    for code in codes:
        bins = [row for row in state["bins"] if row.item_code == code]
        _require(len(bins) == 1 and bins[0].warehouse == WAREHOUSE and bins[0].stock_uom == RAW_UOMS[code]
                 and bins[0].get("company") in (None, "", COMPANY)
                 and _number(bins[0].actual_qty, zero=True) == balances[code], VALUATION)
        _number(bins[0].stock_value, zero=True)
        _number(bins[0].valuation_rate, zero=True)
    return latest


def _raw_valuation_precisions():
    from frappe.model.meta import get_field_precision

    meta = frappe.get_meta("Stock Ledger Entry")
    try:
        values = (get_field_precision(meta.get_field("stock_value")),
                  get_field_precision(meta.get_field("valuation_rate")),
                  int(frappe.db.get_default("float_precision") or 2))
    except (TypeError, ValueError):
        raise ProductionError(VALUATION) from None
    _require(all(type(value) is int and 0 <= value <= 9 for value in values), VALUATION)
    return values


def _raw_valuation_rate_matches(row, precisions):
    """Reject a corrupt positive rate without treating native rounding as loss.

    FIFO derives its average before currency rounding; Moving Average may use
    the configured rounded quantity. Allow those exact rounding envelopes,
    plus a database-decimal ulp, but never recalculate or rewrite ledger cost.
    A zero-quantity native FIFO placeholder may retain its old fallback rate.
    """
    quantity = _number(row.qty_after_transaction, zero=True)
    value = _number(row.stock_value, zero=True)
    rate = _number(row.valuation_rate, zero=True)
    if quantity == 0:
        _require(value == 0, VALUATION)
        return
    value_quantum, rate_quantum, qty_quantum = (Decimal(10) ** -precision for precision in precisions)
    envelope = (value_quantum / 2 + quantity * rate_quantum / 2
                + abs(quantity - quantity.quantize(qty_quantum)) * rate
                + Decimal("0.000000001") * (quantity + 1))
    _require(abs(quantity * rate - value) <= envelope, VALUATION)


def _raw_cache_frozen(state, codes):
    # Native update_bin_qty can change only these derived values and its Bin
    # metadata. Unlike finished-item replay, NO ledger/GL/voucher field moves.
    return {**state, "bins": [{name: value for name, value in row.items()
                               if not (row.item_code in codes and name in (
                                   "stock_value", "valuation_rate", "modified", "modified_by"))}
                              for row in state["bins"]]}


def _settle_raw_bin_values(made, context):
    """Settle deferred native raw Bin values before the invoice can submit.

    ERPNext treats an earlier Manufacture at the same timestamp as "future"
    and can defer this cache write. Quantities and accounting must ALREADY be
    correct. This helper cannot repair inventory, revalue stock, or commit.
    """
    _require(_WRITING.get() is context and isinstance(context, _WriteContext)
             and context.user == frappe.session.user and context.site == SITE
             and context.database is _database() and made, PROTECTED)
    _require(len({wo.name for _plan_value, wo in made}) == len(made), VALUATION)
    when = get_datetime(made[0][0]["posting_datetime"])
    entries, codes = {}, set()
    for plan, wo in made:
        _require(get_datetime(plan["posting_datetime"]) == when and not plan.get("planning_only"), VALUATION)
        _validate_work_order(wo, plan, completed=True)
        entry = _manufacture_for(wo, plan)
        _require(entry.name not in entries, VALUATION)
        for doc in (wo, entry):
            candidates = [owned for owned in context.objects.values()
                          if owned.get("doctype") == doc.doctype and owned.get("name") == doc.name]
            _require(len(candidates) == 1 and candidates[0].get("docstatus") == 1, PROTECTED)
        entries[entry.name] = (plan, wo, entry)
        codes.update(raw["item_code"] for raw in _ingredients(plan))
    codes = tuple(sorted(codes))
    before = _raw_valuation_snapshot(codes, when)
    latest = _raw_valuation_balances(before, codes)
    for name, (plan, wo, entry) in entries.items():
        _require(before["vouchers"].get(("Stock Entry", name)) == entry.as_dict()
                 and before["work_orders"].get(wo.name) == wo.as_dict(), VALUATION)
        movements = [row for row in before["voucher_sles"] if row.voucher_type == "Stock Entry" and row.voucher_no == name]
        _require(len(movements) == len(entry.items)
                 and all(row.company == COMPANY and row.warehouse == WAREHOUSE and not row.is_cancelled
                         and get_datetime(row.posting_datetime) == when for row in movements), VALUATION)
        for part in entry.items:
            rows = [row for row in movements if row.voucher_detail_no == part.name]
            _require(len(rows) == 1 and rows[0].item_code == part.item_code
                     and Decimal(str(rows[0].actual_qty)) == _number(part.transfer_qty) * (1 if part.is_finished_item else -1)
                     and Decimal(str(rows[0].stock_value_difference))
                     == _number(part.basic_amount, zero=True) * (1 if part.is_finished_item else -1), VALUATION)
        _require(sum((Decimal(str(row.stock_value_difference)) for row in movements), Decimal(0)) == 0, VALUATION)
        ledger = [row for row in before["gl"] if row.voucher_type == "Stock Entry" and row.voucher_no == name]
        _require(all(row.company == COMPANY and not row.is_cancelled for row in ledger), VALUATION)
        _require(sum((_number(row.debit, zero=True) for row in ledger), Decimal(0))
                 == sum((_number(row.credit, zero=True) for row in ledger), Decimal(0)), VALUATION)
    from erpnext.stock.stock_balance import update_bin_qty

    changed = []
    with _public_failure(VALUATION):
        for code in codes:
            cached = next(row for row in before["bins"] if row.item_code == code)
            values = {name: latest[code][name] for name in ("stock_value", "valuation_rate")}
            if any(_number(cached[name], zero=True) != _number(value, zero=True) for name, value in values.items()):
                # This native API updates cached Bin data only. In particular
                # do not pass actual_qty, holds, receipts, or financial rows.
                update_bin_qty(code, WAREHOUSE, values)
                changed.append(code)
        after = _raw_valuation_snapshot(codes, when)
        final = _raw_valuation_balances(after, codes)
        _require(_raw_cache_frozen(before, codes) == _raw_cache_frozen(after, codes), VALUATION)
        for code in codes:
            cached = next(row for row in after["bins"] if row.item_code == code)
            _require(all(_number(cached[name], zero=True) == _number(final[code][name], zero=True)
                         for name in ("stock_value", "valuation_rate")), VALUATION)
    return changed


def _valuation_frozen(value):
    """Ignore only native metadata and the replayed dish's derived cost rate."""
    if isinstance(value, dict):
        result = {}
        for key, part in value.items():
            if key in ("modified", "modified_by"):
                continue
            if (key == "incoming_rate" and value.get("doctype") == "Sales Invoice Item"
                    and value.get("item_code") in MENU and value.get("warehouse") == WAREHOUSE):
                continue
            if key == "stock_queue" and isinstance(part, str):
                part = json.loads(part or "[]")
            if (key == "stock_queue" and _number(value.get("qty_after_transaction"), zero=True) == 0
                    and _number(value.get("stock_value"), zero=True) == 0):
                _require(isinstance(part, list) and all(isinstance(layer, list) and len(layer) == 2
                                                       for layer in part), VALUATION)
                quantities = []
                for quantity, rate in part:
                    _require(isinstance(quantity, int | float) and not isinstance(quantity, bool), VALUATION)
                    quantity = Decimal(str(quantity))
                    _require(quantity.is_finite(), VALUATION)
                    _number(rate, zero=True)
                    quantities.append(quantity)
                if all(quantity == 0 for quantity in quantities):
                    # Native replay refreshes the fallback rate of an empty
                    # queue, not the value of any remaining FIFO/LIFO stock.
                    part = [[0, 0] for _layer in part]
            result[key] = _valuation_frozen(part)
        return result
    if isinstance(value, list | tuple):
        return [_valuation_frozen(part) for part in value]
    return value


def _valuation_balances(state, codes):
    """A value-only refresh cannot repair negative, ambiguous or stale quantities."""
    _require(state["sles"] and len(state["sles"]) <= 2000, VALUATION)
    balances, values, latest, seen = {}, {}, {}, set()
    for row in state["previous"]:
        _require(row.item_code in codes and row.item_code not in balances
                 and row.company == COMPANY and row.warehouse == WAREHOUSE and not row.is_cancelled, VALUATION)
        balances[row.item_code] = _number(row.qty_after_transaction, zero=True)
        values[row.item_code] = _number(row.stock_value, zero=True)
        _number(row.valuation_rate, zero=True)
        _require(balances[row.item_code] != 0 or values[row.item_code] == 0, VALUATION)
    for code in codes:
        balances.setdefault(code, Decimal(0))
        values.setdefault(code, Decimal(0))
    for row in state["sles"]:
        identity = (row.item_code, row.posting_datetime, row.creation)
        _require(row.item_code in codes and row.company == COMPANY and row.warehouse == WAREHOUSE
                 and row.voucher_type in ("Stock Entry", "Sales Invoice") and not row.is_cancelled
                 and identity not in seen
                 and not any(row.get(fieldname) for fieldname in (
                     "serial_no", "batch_no", "serial_and_batch_bundle", "has_serial_no", "has_batch_no",
                     "dependant_sle_voucher_detail_no", "is_adjustment_entry")), VALUATION)
        seen.add(identity)
        delta = Decimal(str(row.actual_qty))
        _require(delta.is_finite() and abs(delta) <= Decimal("1e12"), VALUATION)
        balances[row.item_code] += delta
        _require(balances[row.item_code] == _number(row.qty_after_transaction, zero=True), VALUATION)
        value = _number(row.stock_value, zero=True)
        difference = Decimal(str(row.stock_value_difference))
        _require(difference.is_finite() and value - values[row.item_code] == difference, VALUATION)
        values[row.item_code] = value
        _number(row.valuation_rate, zero=True)
        _require(balances[row.item_code] != 0 or value == 0, VALUATION)
        latest[row.item_code] = row
    _require(set(latest) == set(codes), VALUATION)
    for code in codes:
        bins = [row for row in state["bins"] if row.item_code == code]
        _require(len(bins) == 1 and bins[0].warehouse == WAREHOUSE
                 and _number(bins[0].actual_qty, zero=True) == balances[code], VALUATION)
    return latest


def finalize_invoice_valuation(invoice, method=None):
    """Finish the native tied-timestamp Bin refresh in the submitting transaction.

    This is not a historical revaluation service. Existing ledger values and
    accounting must already be correct; a change requiring coordinated repost
    aborts the entire sale. The caller alone owns commit/rollback.
    """
    if not _enabled(invoice.get("company")):
        return
    current = _scoped_rows(invoice)
    traces = [row for row in invoice.get("items") or [] if row.get(KEY_FIELD) or row.get(WORK_ORDER_FIELD)]
    if not current and not traces:
        return
    with _public_failure(VALUATION):
        _require(method == "on_submit" and invoice.get("_action") == "submit"
                 and invoice.get("doctype") == "Sales Invoice" and invoice.get("docstatus") == 1
                 and current and all(row.item_code in MENU for row in traces), PROTECTED)
        _require_schema()
        sale, location = _trusted_source(invoice)
        when = _posted_at(invoice)
        _lock_inventory()
        pairs = _matched_lines(invoice, sale, location)
        _require(len(pairs) == len(current), VALUATION)
        productions = []
        for row, line in pairs:
            plan = _plan(invoice, sale, row, line)
            wo = _existing(plan)
            _require(wo is not None and row.get(WORK_ORDER_FIELD) == wo.name
                     and row.get(KEY_FIELD) == production_key(plan["sale_uuid"], plan["line_uuid"]), VALUATION)
            _assert_lineage(invoice, row, plan, wo)
            _validate_work_order(wo, plan, completed=True)
            productions.append((row, _manufacture_for(wo, plan)))
        codes = tuple(sorted({row.item_code for row in current}))
        before = _valuation_snapshot(codes, when)
        _valuation_balances(before, codes)
        _require(sum(sle.voucher_type == "Sales Invoice" and sle.voucher_no == invoice.name
                     for sle in before["sles"]) == len(current), VALUATION)
        for row, entry in productions:
            issued = [sle for sle in before["sles"] if sle.voucher_type == "Sales Invoice"
                      and sle.voucher_no == invoice.name and sle.voucher_detail_no == row.name]
            made = [sle for sle in before["sles"] if sle.voucher_type == "Stock Entry"
                    and sle.voucher_no == entry.name and sle.item_code == row.item_code]
            _require(len(issued) == len(made) == 1 and issued[0].item_code == row.item_code
                     and Decimal(str(issued[0].actual_qty)) == -_number(row.qty)
                     and Decimal(str(made[0].actual_qty)) == _number(row.qty)
                     and get_datetime(made[0].creation) < get_datetime(issued[0].creation)
                     and get_datetime(issued[0].posting_datetime) == get_datetime(made[0].posting_datetime) == when,
                     VALUATION)
        from erpnext.stock.stock_ledger import update_entries_after

        with _native_writes():
            for code in codes:
                # No sle_id: replay the whole time bucket, including its earlier
                # Manufacture. No repost_doc: native code cannot commit progress.
                update_entries_after({"item_code": code, "warehouse": WAREHOUSE,
                                      "posting_date": when.date(), "posting_time": when.time()},
                                     allow_negative_stock=False)
            after = _valuation_snapshot(codes, when)
            latest = _valuation_balances(after, codes)
            _require(before["gl"] == after["gl"], VALUATION)
            for state in (before, after):
                state["bins"] = [{key: value for key, value in row.items()
                                  if not (row.item_code in codes and key in ("stock_value", "valuation_rate"))}
                                 for row in state["bins"]]
            _require(_valuation_frozen(before) == _valuation_frozen(after), VALUATION)
            for code in codes:
                actual = frappe.db.sql("SELECT actual_qty,stock_value,valuation_rate FROM tabBin "
                    "WHERE item_code=%s AND warehouse=%s FOR UPDATE", (code, WAREHOUSE), as_dict=True)
                _require(len(actual) == 1 and all(_number(actual[0][fieldname], zero=True)
                    == _number(latest[code][sle_field], zero=True) for fieldname, sle_field in (
                        ("actual_qty", "qty_after_transaction"), ("stock_value", "stock_value"),
                        ("valuation_rate", "valuation_rate"))), VALUATION)
        return list(codes)


def guard_invoice_trace(invoice, method=None):
    """Keep native submitted/cancelled line lineage immutable, even if off."""
    if getattr(frappe.local, "site", None) != SITE or not invoice.get("name"):
        return
    if not all(frappe.db.has_column("Sales Invoice Item", name) for name in (KEY_FIELD, WORK_ORDER_FIELD)):
        return
    values = frappe.db.sql("SELECT docstatus FROM `tabSales Invoice` WHERE name=%s FOR UPDATE", (invoice.name,))
    if not values or values[0][0] not in (1, 2):
        return
    previous = frappe.get_doc("Sales Invoice", invoice.name, for_update=True)
    current = [row for row in invoice.get("items") or [] if row.get(KEY_FIELD) or row.get(WORK_ORDER_FIELD)]
    originals = [row for row in previous.get("items") or [] if row.get(KEY_FIELD) or row.get(WORK_ORDER_FIELD)]
    if not current and not originals:
        return
    _require(len(current) == len(originals), PROTECTED)
    for fieldname in ("company", "customer", "is_return", "update_stock", "pos_profile", "set_warehouse", "amended_from"):
        _require(str(invoice.get(fieldname) or "") == str(previous.get(fieldname) or ""), PROTECTED)
    _require(getdate(invoice.posting_date) == getdate(previous.posting_date)
             and get_time(invoice.posting_time) == get_time(previous.posting_time), PROTECTED)
    by_name = {row.name: row for row in originals}
    for row in current:
        original = by_name.get(row.get("name"))
        _require(original is not None, PROTECTED)
        for fieldname in (KEY_FIELD, WORK_ORDER_FIELD, MODIFIER_FIELD, "item_code", "uom", "stock_uom", "warehouse"):
            _require((row.get(fieldname) or "") == (original.get(fieldname) or ""), PROTECTED)
        for fieldname in ("qty", "rate", "conversion_factor"):
            _require(_number(row.get(fieldname), zero=fieldname == "rate")
                     == _number(original.get(fieldname), zero=fieldname == "rate"), PROTECTED)


def _marked_work_order(name):
    if not name or not frappe.db.has_column("Work Order", KEY_FIELD):
        return False
    rows = frappe.db.sql(f"SELECT `{KEY_FIELD}` FROM `tabWork Order` WHERE name=%s FOR UPDATE", (name,))
    return bool(rows and rows[0][0])


def guard_generated_document(doc, method=None):
    """Protect physical production even when the optional sales feature is off."""
    managed = False
    if doc.doctype == "Work Order":
        managed = bool(doc.get(KEY_FIELD)) or _marked_work_order(doc.get("name"))
    elif doc.doctype == "Stock Entry":
        rows = frappe.db.sql("SELECT work_order FROM `tabStock Entry` WHERE name=%s FOR UPDATE", (doc.name,)) if doc.get("name") else []
        previous = rows[0][0] if rows else None
        managed = _marked_work_order(doc.get("work_order")) or _marked_work_order(previous)
    if not managed:
        return
    _require(method != "on_trash", PROTECTED)
    context = _WRITING.get()
    _require(isinstance(context, _WriteContext) and context.user == frappe.session.user
             and context.site == frappe.local.site and context.database is _database()
             and context.objects.get(id(doc)) is doc, PROTECTED)
    if method == "before_cancel":
        _require(context.retiring_pilot is doc and context.user == "Administrator"
                 and context.site == SITE and doc.doctype == "Work Order", PROTECTED)
        # Native Document._cancel sets docstatus=2 before this hook, while
        # WorkOrder.on_cancel has not yet changed its Not Started status.
        _validate_pilot_retirement_work_order(doc, state="cancelling")


def _validate_pilot_retirement_work_order(wo, *, state):
    """Accept only the one untouched internal allocation, never a menu WO."""
    states = {"submitted": (1, "Not Started"), "cancelling": (2, "Not Started"),
              "cancelled": (2, "Cancelled")}
    _require(state in states, PROTECTED)
    try:
        frozen = json.loads(wo.get(SNAPSHOT_FIELD) or "")
    except (TypeError, ValueError):
        raise ProductionError(CHANGED) from None
    _require(_digest(frozen) == _PILOT_PLANNING_SNAPSHOT_SHA256
             and wo.doctype == "Work Order" and wo.name == _PILOT_PLANNING_WORK_ORDER
             and wo.company == COMPANY and (wo.docstatus, wo.status) == states[state]
             and wo.get(KEY_FIELD) == production_key(PILOT["sale_uuid"], PILOT["line_uuid"])
             and wo.get(INVOICE_FIELD) == PILOT["invoice"]
             and wo.production_item == PORTION and wo.bom_no == "BOM-VXN-GOAT-PORTION-001"
             and _number(wo.qty) == 1 and wo.stock_uom == "Nos"
             and wo.source_warehouse == wo.fg_warehouse == WAREHOUSE
             and wo.skip_transfer == 1 and wo.get("transfer_material_against") == "Work Order"
             and get_datetime(wo.planned_start_date) == datetime(2026, 9, 28, 23, 9, 40), CHANGED)
    for name in ("operations", "wip_warehouse", "from_wip_warehouse", "reserve_stock", "use_multi_level_bom",
                 "track_semi_finished_goods", "sales_order", "sales_order_item", "sales_invoice",
                 "material_request", "material_request_item", "production_plan", "production_plan_item",
                 "production_plan_sub_assembly_item", "subcontracting_inward_order",
                 "subcontracting_inward_order_item", "amended_from", "ignore_linked_doctypes"):
        _require(not wo.get(name), CHANGED)
    _require(not (getattr(wo, "flags", None) or {}).get("ignore_links"), PROTECTED)
    for name in ("produced_qty", "material_transferred_for_manufacturing", "additional_transferred_qty",
                 "process_loss_qty", "disassembled_qty"):
        _require(_number(wo.get(name) or 0, zero=True) == 0, CHANGED)
    rows = wo.get("required_items") or []
    _require(len(rows) == 1 and rows[0].item_code == RAW and rows[0].stock_uom == "Kg"
             and rows[0].source_warehouse == WAREHOUSE
             and _number(rows[0].required_qty) == PER_PORTION
             and rows[0].get("include_item_in_manufacturing") == 1, CHANGED)
    for name in ("consumed_qty", "transferred_qty", "returned_qty", "stock_reserved_qty"):
        _require(_number(rows[0].get(name) or 0, zero=True) == 0, CHANGED)
    for name in ("operation", "operation_row_id", "allow_alternative_item", "is_additional_item",
                 "is_customer_provided_item", "voucher_detail_reference"):
        _require(not rows[0].get(name), CHANGED)


def _assert_pilot_retirement_no_activity(wo, *, original_invoice=False):
    """Lock absence checks; even draft/cancelled production history forbids it."""
    # A Purchase Material Request was separately created from this pilot.
    # It is procurement, not a WO reservation, and may not be silently dropped
    # or detached. Require its explicit native resolution BEFORE WO.cancel
    # can temporarily update any native status/hold. Keep cancelled history.
    requests = frappe.db.sql("SELECT name,docstatus FROM `tabMaterial Request` "
        "WHERE work_order=%s ORDER BY name FOR UPDATE", (wo.name,))
    _require(all(row[1] == 2 for row in requests),
             "This planning Work Order has an active Material Request. Review that request before retiring the plan.")
    for doctype, fieldname in (("Stock Entry", "work_order"), ("Job Card", "work_order"),
                              ("Stock Reservation Entry", "voucher_no"),
                              ("Stock Ledger Entry", "voucher_no")):
        _require(not frappe.db.sql(f"SELECT name FROM `tab{doctype}` WHERE `{fieldname}`=%s FOR UPDATE",
                                   (wo.name,)), CHANGED)
    _require(not frappe.db.sql("SELECT name FROM `tabGL Entry` WHERE voucher_no=%s OR against_voucher=%s FOR UPDATE",
                               (wo.name, wo.name)), CHANGED)
    # This allocation was planning-only and has NEVER funded an invoice row,
    # including an invoice since cancelled. A new dish must use its own UUID.
    _require(not frappe.db.sql(f"SELECT name FROM `tabSales Invoice Item` WHERE `{KEY_FIELD}`=%s "
                               f"OR `{WORK_ORDER_FIELD}`=%s FOR UPDATE", (wo.get(KEY_FIELD), wo.name)), CHANGED)
    if original_invoice:
        _require(not frappe.db.sql("SELECT name FROM `tabStock Ledger Entry` "
            "WHERE voucher_type='Sales Invoice' AND voucher_no=%s FOR UPDATE", (PILOT["invoice"],)), CHANGED)
        _require(not frappe.db.sql("SELECT name FROM `tabGL Entry` "
            "WHERE (voucher_type='Sales Invoice' AND voucher_no=%s) "
            "OR (against_voucher_type='Sales Invoice' AND against_voucher=%s) FOR UPDATE",
            (PILOT["invoice"], PILOT["invoice"])), CHANGED)


def _validate_pilot_retirement_source(invoice, sale, location):
    from kit_pos.connector import ids

    _require(invoice.docstatus == 0 and invoice.get("status") == "Draft"
             and not invoice.get("amended_from") and _number(invoice.grand_total) == 49000
             and _number(invoice.get("paid_amount"), zero=True) == 0
             and not invoice.get("payments") and _number(sale.final_total) == 49000
             and sale.is_suspend == 1 and sale.payment_status == "due"
             and _number(sale.get("amount_paid"), zero=True) == 0 and not sale.get("payments")
             and _posted_at(invoice) == datetime(2026, 9, 28, 23, 9, 40), CHANGED)
    lines = [row for row in sale.lines if str(row.line_id) == PILOT["line_id"]
             or row.get("desktop_line_uuid") == PILOT["line_uuid"]]
    _require(len(lines) == 1 and str(lines[0].line_id) == PILOT["line_id"]
             and lines[0].desktop_line_uuid == PILOT["line_uuid"]
             and lines[0].get("item_code") == PILOT["item_code"]
             and _number(lines[0].quantity) == 1
             and _number(lines[0].unit_price) == 25000
             and _number(lines[0].unit_price_inc_tax) == 25000, CHANGED)
    for fieldname in ("product_id", "variation_id"):
        _require(str(lines[0].get(fieldname)) == "554"
                 and ids.lookup("Item", lines[0].get(fieldname)) == PILOT["item_code"], CHANGED)
    rows = [row for row in invoice.items if row.item_code == PILOT["item_code"]]
    _require(len(rows) == 1 and _number(rows[0].qty) == 1 and _number(rows[0].rate) == 25000
             and rows[0].uom == rows[0].stock_uom == "Pc(s)" and _number(rows[0].conversion_factor) == 1
             and not rows[0].get(MODIFIER_FIELD), CHANGED)
    _require(not _matched_lines(invoice, sale, location), CHANGED)
    _require(not frappe.db.get_value("Item", PILOT["item_code"], "is_stock_item"), CHANGED)


def _pilot_retirement_bins():
    result = {}
    for code in (RAW, PORTION):
        rows = frappe.db.sql("SELECT actual_qty,stock_value,valuation_rate,reserved_qty_for_production,planned_qty "
            "FROM tabBin WHERE item_code=%s AND warehouse=%s FOR UPDATE", (code, WAREHOUSE))
        _require(len(rows) == 1, CHANGED)
        result[code] = tuple(_number(value, zero=True) for value in rows[0])
    return result


def _retire_legacy_pilot_work_order():
    """Native cancellation of ONLY the confirmed, never-produced legacy pilot.

    Private/unwhitelisted, no commit or new production. The caller owns its
    backup, rollback and atomic source-line removal/replacement. Run BEFORE
    correcting the original 49,000 held/unpaid draft. The cancelled original
    retains its key and frozen snapshot forever; a replacement needs its own
    genuine line UUID. An exact already-cancelled original is a verified no-op
    even after the bill is corrected. Never manually subtract native Bin holds.
    """
    _require(frappe.session.user == "Administrator" and frappe.local.site == SITE, PROTECTED)
    _require_schema()
    from kit_pos.connector.materialize import _locked_sale

    # Re-enter the caller's lock order, rather than taking a WO before the
    # opening/sale/invoice fence used by settlement and cash-register closing.
    sale = _locked_sale(PILOT["sale"])
    invoice = frappe.get_doc("Sales Invoice", PILOT["invoice"], for_update=True)
    _require(str(sale.name) == PILOT["sale"] and sale.sale_uuid == PILOT["sale_uuid"]
             and sale.sales_invoice == PILOT["invoice"] and sale.invoice_no == "KOD-2-101"
             and invoice.name == PILOT["invoice"] and invoice.company == COMPANY, CHANGED)
    _lock_inventory()
    wo = frappe.get_doc("Work Order", _PILOT_PLANNING_WORK_ORDER, for_update=True)
    already_cancelled = wo.docstatus == 2
    _validate_pilot_retirement_work_order(wo, state="cancelled" if already_cancelled else "submitted")
    _assert_pilot_retirement_no_activity(wo, original_invoice=not already_cancelled)
    # Native cancellation otherwise discovers static/dynamic backlinks only
    # AFTER on_cancel. Preflight them too, without suppressing the final check.
    wo.check_no_back_links_exist()
    result = {"work_order": wo.name, "already_cancelled": already_cancelled,
              "production_key": wo.get(KEY_FIELD),
              "raw_reservation_released": "0", "finished_planned_quantity_released": "0"}
    if already_cancelled:
        return result
    sale, location = _trusted_source(invoice, settled=False)
    _validate_pilot_retirement_source(invoice, sale, location)
    before_documents = _digest([sale.as_dict(), invoice.as_dict()])
    original_snapshot_text = wo.get(SNAPSHOT_FIELD)
    before_bins = _pilot_retirement_bins()
    _require(before_bins[RAW][3] >= PER_PORTION and before_bins[PORTION][4] >= 1, CHANGED)
    with _native_writes() as context:
        context.objects[id(wo)] = wo
        context.retiring_pilot = wo
        wo.cancel()
        context.retiring_pilot = None
        current = frappe.get_doc("Work Order", wo.name, for_update=True)
        _validate_pilot_retirement_work_order(current, state="cancelled")
        _require(current.get(SNAPSHOT_FIELD) == wo.get(SNAPSHOT_FIELD) == original_snapshot_text, CHANGED)
        _assert_pilot_retirement_no_activity(current, original_invoice=True)
        after_bins = _pilot_retirement_bins()
        _require(after_bins[RAW] == (*before_bins[RAW][:3], before_bins[RAW][3] - PER_PORTION,
                                     before_bins[RAW][4])
                 and after_bins[PORTION] == (*before_bins[PORTION][:4], before_bins[PORTION][4] - 1), CHANGED)
        after_documents = _digest([
            frappe.get_doc("Kit POS Sale", sale.name, for_update=True).as_dict(),
            frappe.get_doc("Sales Invoice", invoice.name, for_update=True).as_dict(),
        ])
        _require(after_documents == before_documents, CHANGED)
    result.update(raw_reservation_released="0.4", finished_planned_quantity_released="1")
    return result


def _pilot_item_and_bom():
    """Only the dedicated internal allocation item may be created here."""
    if not frappe.db.exists("Item", PORTION):
        item = frappe.get_doc({"doctype": "Item", "item_code": PORTION,
            "item_name": "Internal Goat Allocation Portion", "stock_uom": "Nos",
            "item_group": "VXN Kitchen Ingredients", "is_stock_item": 1, "is_sales_item": 0,
            "is_purchase_item": 0, "include_item_in_manufacturing": 1, "disabled": 0,
            "description": "One internal goat allocation uses 0.4 Kg raw goat; no preparation style, side or cooked-weight yield is assumed.",
            "uoms": [{"uom": "Nos", "conversion_factor": 1}],
            "item_defaults": [{"company": COMPANY, "default_warehouse": WAREHOUSE,
                "buying_cost_center": COST_CENTER, "selling_cost_center": COST_CENTER,
                "expense_account": "5111 - Cost of Goods Sold - VXN"}]})
        item.insert()
    item = frappe.get_doc("Item", PORTION)
    _require(item.is_stock_item == 1 and not item.is_sales_item and not item.is_purchase_item
             and not item.disabled and item.stock_uom == "Nos" and item.item_group == "VXN Kitchen Ingredients", CHANGED)
    names = frappe.get_all("BOM", filters={"item": PORTION}, pluck="name", limit_page_length=0)
    _require(len(names) <= 1, CHANGED)
    if names:
        bom = frappe.get_doc("BOM", names[0])
    else:
        bom = frappe.get_doc({"doctype": "BOM", "company": COMPANY, "item": PORTION, "quantity": 1,
            "currency": "UGX", "conversion_rate": 1, "is_active": 1, "is_default": 1,
            "with_operations": 0, "rm_cost_as_per": "Valuation Rate", "transfer_material_against": "Work Order",
            "backflush_based_on": "BOM", "items": [{"item_code": RAW, "qty": 0.4, "uom": "Kg",
                "stock_uom": "Kg", "conversion_factor": 1, "source_warehouse": WAREHOUSE,
                "include_item_in_manufacturing": 1}]})
        bom.insert()
        bom.submit()
    _require(bom.company == COMPANY and bom.item == PORTION and bom.docstatus == 1 and bom.is_active
             and _number(bom.quantity) == 1 and bom.uom == "Nos" and len(bom.items) == 1
             and bom.items[0].item_code == RAW and _number(bom.items[0].stock_qty) == PER_PORTION
             and bom.items[0].stock_uom == "Kg" and bom.items[0].source_warehouse == WAREHOUSE
             and not bom.operations and not bom.get("secondary_items") and not bom.get("process_loss_qty"), CHANGED)
    return {"bom": bom.name, "modified": str(bom.modified), "quantity": "1", "uom": "Nos",
            "raw_item": RAW, "raw_qty": "0.4", "raw_uom": "Kg", "raw_per_portion": "0.4", "warehouse": WAREHOUSE}


def plan_legacy_pilot():
    """Administrator-only instruction for exact held KOD-2-101; NEVER cook it.

    Caller must take/review its backup and exact source snapshot, then own the
    final commit. This is a write helper, NOT a preview. Native WO submission
    reserves/plans stock, but creates no Stock Entry, SLE, GL, invoice or tender.
    """
    _require(frappe.session.user == "Administrator" and _enabled(COMPANY), UNAVAILABLE)
    from kit_pos.connector import ids

    _require_schema()
    invoice = frappe.get_doc("Sales Invoice", PILOT["invoice"])
    sale, location = _trusted_source(invoice, settled=False)
    _require(str(sale.name) == PILOT["sale"] and sale.sale_uuid == PILOT["sale_uuid"]
             and sale.sales_invoice == PILOT["invoice"] and sale.invoice_no == "KOD-2-101"
             and invoice.docstatus == 0 and invoice.grand_total == 49000 and invoice.paid_amount == 0
             and _number(sale.final_total) == 49000
             and sale.is_suspend == 1 and sale.payment_status == "due" and not sale.amount_paid)
    lines = [row for row in sale.lines if str(row.line_id) == PILOT["line_id"]]
    _require(len(lines) == 1 and lines[0].desktop_line_uuid == PILOT["line_uuid"]
             and _number(lines[0].quantity) == 1
             and _number(lines[0].get("unit_price_inc_tax") or lines[0].get("unit_price")) == 25000)
    for fieldname in ("product_id", "variation_id"):
        _require(str(lines[0].get(fieldname)) == "554"
                 and ids.lookup("Item", lines[0].get(fieldname)) == PILOT["item_code"])
    _require(not lines[0].get("item_code") or lines[0].get("item_code") == PILOT["item_code"])
    rows = [row for row in invoice.items if row.item_code == PILOT["item_code"]]
    _require(len(rows) == 1 and _number(rows[0].qty) == 1 and _number(rows[0].rate) == 25000 and rows[0].uom == "Pc(s)"
             and rows[0].stock_uom == "Pc(s)" and _number(rows[0].conversion_factor) == 1)
    _require(not frappe.db.get_value("Item", PILOT["item_code"], "is_stock_item"), CHANGED)
    _require(not frappe.db.get_single_value("Stock Settings", "auto_reserve_stock"), UNAVAILABLE)
    when = _posted_at(invoice)
    _lock_inventory()
    with _native_writes() as context:
        recipe = _pilot_item_and_bom()
        plan = {"version": 1, "company": COMPANY, "sale": PILOT["sale"], "sale_uuid": PILOT["sale_uuid"],
            "sale_label": "KOD-2-101",
            "line_id": PILOT["line_id"], "line_uuid": PILOT["line_uuid"], "source_item": PILOT["item_code"],
            "production_item": PORTION, "quantity": "1", "uom": "Nos", "raw_quantity": "0.4",
            "posting_datetime": str(when), "recipe": recipe, "modifier_reference": "", "instructions": [], "planning_only": True}
        existing = _existing(plan)
        if existing:
            _validate_work_order(existing, plan, completed=False)
            _require(not frappe.db.exists("Stock Entry", {"work_order": existing.name}), CHANGED)
            return existing.name
        _require(_available(RAW, when, future_floor=True) >= PER_PORTION, SHORTAGE)
        before = (frappe.get_doc("Kit POS Sale", sale.name).as_dict(), invoice.as_dict(),
                  frappe.db.get_value("Bin", {"item_code": RAW, "warehouse": WAREHOUSE},
                                      ["actual_qty", "valuation_rate", "stock_value"]))
        wo = _new_work_order(invoice, plan, context)
        after = (frappe.get_doc("Kit POS Sale", sale.name).as_dict(),
                 frappe.get_doc("Sales Invoice", invoice.name).as_dict(),
                 frappe.db.get_value("Bin", {"item_code": RAW, "warehouse": WAREHOUSE},
                                     ["actual_qty", "valuation_rate", "stock_value"]))
        _require(_digest(before) == _digest(after) and not frappe.db.exists("Stock Entry", {"work_order": wo.name}), CHANGED)
        return wo.name
