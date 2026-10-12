"""Private, read-only made-to-order availability for the Vox kitchen.

Only dish counts leave this module. Native Work Orders and Stock Reservation
Entries are authoritative; this module never updates a Bin or allocates stock.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_FLOOR

import frappe

from fours_customizations.vox_kitchen_recipes import COMPONENTS, FINISHED_UOM, MENU, RAW_UOMS

CONFIG = "vox_goat_work_orders"
SITE = "multax.kit.africa"
COMPANY = "Vox Lounge Nansana"
WAREHOUSE = "Kitchen - VXN"
RAW_ITEM = "VXN-RAW-GOAT-KG"
MENU_ITEMS = frozenset(MENU)
PER_DISH = Decimal("0.4")
ZERO = Decimal("0")
# Native stock quantities may be rounded to six decimal places in Bin.
_TOLERANCE = Decimal("0.0000005")
_OTHER_FORECASTS = (
    "reserved_qty", "reserved_qty_for_sub_contract", "reserved_qty_for_production_plan",
)


def _number(value, *, negative=False):
    if isinstance(value, bool):
        raise ValueError("Invalid native stock quantity")
    try:
        result = Decimal(str(0 if value is None else value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Invalid native stock quantity") from None
    if not result.is_finite() or (not negative and result < 0):
        raise ValueError("Invalid native stock quantity")
    return result


def _enabled(company):
    if company != COMPANY or getattr(getattr(frappe, "local", None), "site", None) != SITE:
        return False
    config = frappe.conf.get(CONFIG, {})
    if not isinstance(config, dict):
        raise ValueError("Invalid goat workflow configuration")
    enabled = config.get(COMPANY, False)
    if type(enabled) not in (bool, int) or enabled not in (0, 1):
        raise ValueError("Invalid goat workflow configuration")
    if not enabled:
        return False
    # Same supersede switch as vox_work_orders._enabled: the provider returns {}
    # while Kit POS BOM production is on or paused for this company.
    try:
        from kit_pos.connector import production

        policy = production.engine_policy(company)
        if policy is not None:
            read = policy.get if isinstance(policy, dict) else lambda key: getattr(policy, key, None)
            if read("supersedes_vox_pilot") is True and read("mode") in ("on", "paused"):
                return False
    except Exception:
        pass
    return True


def _identity(value):
    if not isinstance(value, str) or not value or len(value) > 140 or value != value.strip():
        raise ValueError("Ambiguous native stock reservation")
    return value


def _bin(item_code, warehouse, *, for_update=False):
    suffix = " FOR UPDATE" if for_update else ""
    rows = frappe.db.sql(
        """SELECT name, actual_qty, reserved_qty, reserved_qty_for_production,
                  reserved_qty_for_sub_contract, reserved_qty_for_production_plan,
                  reserved_stock
             FROM `tabBin`
            WHERE item_code = %s AND warehouse = %s""" + suffix,
        (item_code, warehouse), as_dict=True,
    )
    if len(rows) > 1:
        raise ValueError("Ambiguous native stock bin")
    return rows[0] if rows else {}


def reservation_hold(item_code, warehouse, *, exclude_work_order=None, for_update=False):
    """Native outstanding raw hold, de-duplicated by exact Work Order row.

The caller performing stock writes MUST first lock the existing shared raw Bin
and retain that lock through its final native stock transaction. for_update=True
also makes these reads current/locking rather than an earlier MVCC snapshot.
Only that caller's Work Order (including its correlated SREs) may be excluded.

Sales/subcontract/production-plan forecast reservations require a separate
correlation policy. This narrowly scoped pilot fails closed if any exist rather
than silently ignoring them or double-counting a corresponding stock entry.
"""
    _identity(item_code)
    _identity(warehouse)
    if exclude_work_order is not None:
        _identity(exclude_work_order)
    if type(for_update) is not bool:
        raise ValueError("Invalid stock read mode")
    native_bin = _bin(item_code, warehouse, for_update=for_update)
    if any(_number(native_bin.get(field)) > 0 for field in _OTHER_FORECASTS):
        raise ValueError("Uncorrelated native stock forecast reservation")
    suffix = " FOR UPDATE" if for_update else ""
    work_orders = frappe.db.sql(
        """SELECT wo.name AS work_order, wi.name AS detail, wo.skip_transfer,
                  wi.required_qty, wi.transferred_qty, wi.consumed_qty
             FROM `tabWork Order` wo
             JOIN `tabWork Order Item` wi ON wi.parent = wo.name
            WHERE wo.docstatus = 1 AND wo.status NOT IN ('Stopped', 'Completed', 'Closed')
              AND wi.parenttype = 'Work Order' AND wi.parentfield = 'required_items'
              AND wi.item_code = %s AND wi.source_warehouse = %s
              AND (wi.required_qty > wi.transferred_qty OR wi.required_qty > wi.consumed_qty)
            ORDER BY wo.name, wi.name""" + suffix,
        (item_code, warehouse), as_dict=True,
    )
    planned = {}
    for row in work_orders:
        key = (_identity(row.get("work_order")), _identity(row.get("detail")))
        if key in planned or row.get("skip_transfer") not in (0, 1):
            raise ValueError("Ambiguous native production reservation")
        required = _number(row.get("required_qty"))
        used = _number(row.get("consumed_qty" if row.get("skip_transfer") else "transferred_qty"))
        planned[key] = max(ZERO, required - used)
    native_planned = _number(native_bin.get("reserved_qty_for_production"))
    if abs(sum(planned.values(), ZERO) - native_planned) > _TOLERANCE:
        raise ValueError("Native production reservation needs reconciliation")

    reservations = frappe.db.sql(
        """SELECT name, voucher_type, voucher_no, voucher_detail_no,
                  reserved_qty, delivered_qty, transferred_qty, consumed_qty
             FROM `tabStock Reservation Entry`
            WHERE docstatus = 1 AND item_code = %s AND warehouse = %s
              AND delivered_qty < reserved_qty
            ORDER BY name""" + suffix,
        (item_code, warehouse), as_dict=True,
    )
    stock_total, independent = ZERO, ZERO
    correlated = {}
    seen = set()
    for row in reservations:
        name = _identity(row.get("name"))
        if name in seen:
            raise ValueError("Duplicate native stock reservation")
        seen.add(name)
        remaining = _number(row.get("reserved_qty")) - sum(
            (_number(row.get(field)) for field in ("delivered_qty", "transferred_qty", "consumed_qty")),
            ZERO,
        )
        if remaining < -_TOLERANCE:
            raise ValueError("Over-consumed native stock reservation")
        remaining = max(ZERO, remaining)
        stock_total += remaining
        if not remaining:
            continue
        if row.get("voucher_type") == "Work Order":
            key = (_identity(row.get("voucher_no")), _identity(row.get("voucher_detail_no")))
            correlated[key] = correlated.get(key, ZERO) + remaining
        else:
            independent += remaining
    if abs(stock_total - _number(native_bin.get("reserved_stock"))) > _TOLERANCE:
        raise ValueError("Native stock reservation needs reconciliation")
    for key in planned.keys() | correlated.keys():
        if key[0] != exclude_work_order:
            # A WO and its stock reservations represent the SAME demand. An
            # independent SRE/WO, including the other preparation, remains held.
            independent += max(planned.get(key, ZERO), correlated.get(key, ZERO))
    return independent


def catalogue_availability(*, company, location, item_codes):
    """Project dish stock-UOM counts only; never expose ingredients or recipes."""
    if not _enabled(company):
        return {}
    if not isinstance(location, dict) or location.get("company") != company:
        raise ValueError("Mismatched catalogue company")
    if not isinstance(item_codes, (list, tuple)) or any(not isinstance(code, str) for code in item_codes):
        raise ValueError("Invalid catalogue identities")
    codes = MENU_ITEMS.intersection(item_codes)
    if not codes:
        return {}
    warehouse = frappe.db.get_value("Warehouse", WAREHOUSE, ["company", "is_group", "disabled"], as_dict=True)
    if not warehouse or warehouse.get("company") != COMPANY or warehouse.get("is_group") or warehouse.get("disabled"):
        raise ValueError("Invalid production warehouse")
    raw_codes = sorted({component[0] for code in codes for component in COMPONENTS[code]})
    for code in (*raw_codes, *sorted(codes)):
        item = frappe.db.get_value(
            "Item", code, ["stock_uom", "is_stock_item", "disabled", "custom_company"], as_dict=True,
        )
        expected_uom = RAW_UOMS[code] if code in raw_codes else FINISHED_UOM
        if (
            not item or item.get("disabled") or item.get("is_stock_item") != 1
            or item.get("stock_uom") != expected_uom
            or item.get("custom_company") not in (None, "", COMPANY)
        ):
            raise ValueError("Invalid production stock unit")
    # These per-plate recipes consume fractions of the raw stock unit. Do not
    # advertise stock if its native UOM would reject the actual manufacture.
    for uom in sorted({RAW_UOMS[code] for code in raw_codes}):
        native_uom = frappe.db.get_value(
            "UOM", uom, ["enabled", "must_be_whole_number"], as_dict=True,
        )
        if not native_uom or native_uom.get("enabled") != 1 or native_uom.get("must_be_whole_number") != 0:
            raise ValueError("Invalid production stock unit")
    available_raw = {}
    for code in raw_codes:
        raw = _number(_bin(code, WAREHOUSE).get("actual_qty"), negative=True)
        held = reservation_hold(code, WAREHOUSE)
        available_raw[code] = max(ZERO, raw - held)
    # Fresh lines always get their own origin-bound WO/Manufacture. Existing
    # finished goods belong to an already prepared order and may only be reused
    # by its verified payment amendment, not allocated to a different new line.
    # Returned/unexplained finished stock needs review first: otherwise a native
    # non-batched valuation queue can mix origins even after new manufacture.
    # Each dish independently draws from its shared ingredient pools. A bowl
    # needs BOTH ingredients; capacities of alternatives must not be summed.
    result = {}
    for code in sorted(codes):
        finished = _number(_bin(code, WAREHOUSE).get("actual_qty"), negative=True)
        capacity = min(
            (available_raw[raw_code] / per_plate).to_integral_value(rounding=ROUND_FLOOR)
            for raw_code, _uom, per_plate in COMPONENTS[code]
        )
        result[code] = {"qty_available": 0.0 if finished != 0 else float(capacity)}
    return result
