"""
sales_chain_crush_test.py — Fours Customizations
================================================

Randomised crush test for the sales chain: invoices, price corrections, goods
returns, cancellations and amendments in every order they can occur.

Run it after **any** change to price_adjustment, delivery_note_handler or
sales_chain_integrity::

    bench --site multax.kit.africa console
    >>> from fours_customizations.sales_chain_crush_test import crush
    >>> crush(n=120)

Every scenario builds a fresh sale, applies a sequence of operations, checks
the invariants below, then rolls back — so it is safe to run against
production and leaves nothing behind but the test customer.

**The governing invariant**

    what the customer owes  ==  (units they still hold) x (price after corrections)

If that holds after any sequence, the module cannot leak money or stock. The
others guard the ways it has actually gone wrong before:

  * every voucher balances;
  * the warehouse moved by exactly the quantity the customer holds;
  * a cancelled goods return never leaves its credit note standing;
  * the price in force is the last price anyone asked for;
  * an invoice never grows a second live delivery note.

Each operation runs inside a savepoint, so an operation the system *refuses*
leaves no trace — exactly as a rejected request would.
"""

import itertools
import random
import traceback

import frappe
from frappe.utils import flt

COMPANY = "4S Industries Limited"
WAREHOUSE = "Main Store - 4S"
CUSTOMER = "ZZ CRUSH TEST"
ITEM = "4S-071"
APPROVER = "mosesm@gmail.com"
STORE = "fauzianayiga1234@gmail.com"
CLERK = "moureenk90@gmail.com"
REASON = ("Crush test correction: aligning the unit rate to the agreed trade price "
          "confirmed by the sales manager for this delivery.")

QTY = 100
RATE = 1000.0


def _silence_slack():
    import fours_customizations.notifications as notif
    notif.send_slack = lambda message, attachments=None: None


def setup():
    """Create the test customer once (committed so it can be inspected)."""
    if not frappe.db.exists("Customer", CUSTOMER):
        frappe.get_doc({
            "doctype": "Customer",
            "customer_name": CUSTOMER,
            "customer_group": frappe.get_all("Customer Group", filters={"is_group": 0}, pluck="name")[0],
            "territory": frappe.get_all("Territory", filters={"is_group": 0}, pluck="name")[0],
            "custom_allow_credit": 1,
        }).insert(ignore_permissions=True)
        frappe.db.commit()
        print(f"created customer {CUSTOMER}")
    else:
        frappe.db.set_value("Customer", CUSTOMER, "custom_allow_credit", 1)
        frappe.db.commit()
        print(f"customer {CUSTOMER} ready")


# ── building a sale ──────────────────────────────────────────────────────────

def new_sale(qty=QTY, rate=RATE):
    """Submitted invoice + submitted delivery note. Returns (si, dn)."""
    si = frappe.new_doc("Sales Invoice")
    si.company = COMPANY
    si.customer = CUSTOMER
    si.set_posting_time = 1
    si.posting_date = frappe.utils.today()
    si.update_stock = 0
    si.is_pos = 0
    si.append("items", {"item_code": ITEM, "qty": qty, "rate": rate, "warehouse": WAREHOUSE})
    si.flags.ignore_permissions = True
    si.insert()
    si.submit()

    dn_names = frappe.get_all("Delivery Note Item", filters={"against_sales_invoice": si.name},
                              pluck="parent", distinct=True)
    if not dn_names:
        raise AssertionError("no delivery note was auto-created")
    dn = frappe.get_doc("Delivery Note", dn_names[0])
    dn.flags.ignore_permissions = True
    dn.submit()
    return si.name, dn.name


# ── operations ───────────────────────────────────────────────────────────────

def _as(user, fn):
    original = frappe.session.user
    try:
        frappe.set_user(user)
        return fn()
    finally:
        frappe.set_user(original)


def op_adjust(si_name, delta, user=APPROVER):
    """Move the unit price by `delta` (negative lowers it)."""
    from fours_customizations.price_adjustment import create_price_adjustment, effective_rates
    si = frappe.get_doc("Sales Invoice", si_name)
    row = si.items[0]
    current = effective_rates(si_name)[row.name]
    return _as(user, lambda: create_price_adjustment(
        si_name, [{"item_row": row.name, "new_rate": flt(current) + delta}], REASON))


def op_return(si_name, dn_name, qty):
    """Store keeper posts `qty` units back."""
    from erpnext.controllers.sales_and_purchase_return import make_return_doc

    def _do():
        dn_ret = make_return_doc("Delivery Note", dn_name)
        keep = dn_ret.items[:1]
        keep[0].qty = -abs(qty)
        dn_ret.set("items", keep)
        dn_ret.items[0].idx = 1
        dn_ret.set_posting_time = 1
        dn_ret.posting_date = frappe.utils.today()
        dn_ret.insert()
        dn_ret.submit()
        return dn_ret.name
    return _as(STORE, _do)


def op_cancel_return(dn_return):
    doc = frappe.get_doc("Delivery Note", dn_return)
    doc.flags.ignore_permissions = True
    doc.cancel()
    return dn_return


def op_amend_return(dn_return, qty=None):
    src = frappe.get_doc("Delivery Note", dn_return)
    amended = frappe.copy_doc(src)
    amended.amended_from = dn_return
    amended.docstatus = 0
    if qty is not None:
        amended.items[0].qty = -abs(qty)
    amended.set_posting_time = 1
    amended.posting_date = frappe.utils.today()
    amended.flags.ignore_permissions = True
    amended.insert()
    amended.submit()
    return amended.name


def op_cancel_credit(si_name):
    """Try to cancel the newest submitted credit note tied to this sale."""
    names = frappe.get_all("Sales Invoice",
                           filters={"is_return": 1, "docstatus": 1, "customer": CUSTOMER},
                           or_filters={"return_against": si_name, "custom_price_adjustment_for": si_name},
                           order_by="creation desc", pluck="name")
    if not names:
        return None
    doc = frappe.get_doc("Sales Invoice", names[0])
    doc.flags.ignore_permissions = True
    doc.cancel()
    return names[0]


def op_cancel_invoice(si_name):
    doc = frappe.get_doc("Sales Invoice", si_name)
    doc.flags.ignore_permissions = True
    doc.cancel()
    return si_name


# ── invariants ───────────────────────────────────────────────────────────────

def related_vouchers(si_name):
    """Every Sales Invoice document belonging to this sale."""
    names = {si_name}
    names |= set(frappe.get_all("Sales Invoice",
                                filters={"return_against": si_name}, pluck="name"))
    names |= set(frappe.get_all("Sales Invoice",
                                filters={"custom_price_adjustment_for": si_name}, pluck="name"))
    return names


def check(si_name, dn_name, opened_stock, intended_rate=None):
    """Return a list of invariant violations."""
    from fours_customizations.price_adjustment import effective_rates

    problems = []
    si = frappe.get_doc("Sales Invoice", si_name)
    row = si.items[0]

    # --- units the customer still holds -------------------------------------
    returned = 0.0
    for dnr in frappe.get_all("Delivery Note",
                              filters={"return_against": dn_name, "docstatus": 1, "is_return": 1},
                              pluck="name"):
        for it in frappe.get_all("Delivery Note Item", filters={"parent": dnr},
                                 fields=["qty"]):
            returned += abs(flt(it.qty))
    delivered = flt(row.qty) if si.docstatus == 1 else 0.0
    held = delivered - returned

    # --- what the books say the customer owes -------------------------------
    vouchers = related_vouchers(si_name)
    receivable = 0.0
    for v in vouchers:
        rows = frappe.db.sql(
            """SELECT COALESCE(SUM(gle.debit - gle.credit),0)
               FROM `tabGL Entry` gle INNER JOIN `tabAccount` a ON a.name = gle.account
               WHERE gle.voucher_no=%s AND gle.is_cancelled=0 AND a.account_type='Receivable'""",
            (v,))
        receivable += flt(rows[0][0]) if rows else 0.0

    eff = effective_rates(si_name).get(row.name, flt(row.rate)) if si.docstatus == 1 else 0.0

    # The price in force must be the last price anyone asked for. Comparing the
    # books against effective_rates alone cannot catch a compounding error,
    # because both sides would drift together.
    if intended_rate is not None and si.docstatus == 1 and abs(flt(eff) - flt(intended_rate)) > 0.01:
        problems.append(
            f"PRICE DRIFT: effective rate is {flt(eff):,.2f} but the last correction "
            f"asked for {flt(intended_rate):,.2f}")
    expected = held * flt(eff)

    if abs(receivable - expected) > 1.0:
        problems.append(
            f"RECEIVABLE MISMATCH: books say {receivable:,.2f}, "
            f"goods held {held:g} x effective {flt(eff):,.2f} = {expected:,.2f} "
            f"(diff {receivable - expected:,.2f})")

    # --- every voucher balances ---------------------------------------------
    for v in vouchers:
        r = frappe.db.sql("""SELECT COALESCE(SUM(debit),0), COALESCE(SUM(credit),0)
                             FROM `tabGL Entry` WHERE voucher_no=%s AND is_cancelled=0""", (v,))
        if r and abs(flt(r[0][0]) - flt(r[0][1])) > 0.01:
            problems.append(f"UNBALANCED GL on {v}: Dr {flt(r[0][0]):,.2f} Cr {flt(r[0][1]):,.2f}")

    # --- stock actually moved by the same amount ----------------------------
    now_stock = flt(frappe.db.get_value("Bin", {"item_code": ITEM, "warehouse": WAREHOUSE},
                                        "actual_qty"))
    moved_out = opened_stock - now_stock
    if abs(moved_out - held) > 0.001:
        problems.append(
            f"STOCK MISMATCH: warehouse fell by {moved_out:g}, customer holds {held:g}")

    # --- a cancelled goods return must not leave its credit note standing ---
    for dnr in frappe.get_all("Delivery Note",
                              filters={"return_against": dn_name, "is_return": 1, "docstatus": 2},
                              pluck="name"):
        for cn in frappe.get_all("Sales Invoice Item",
                                 filters={"delivery_note": dnr}, pluck="parent", distinct=True):
            if frappe.db.get_value("Sales Invoice", cn, "docstatus") == 1:
                problems.append(
                    f"ORPHAN CREDIT NOTE: {cn} still submitted although its goods return "
                    f"{dnr} was cancelled (customer credited for goods they kept)")

    # --- never more than one live forward delivery note ---------------------
    fwd = frappe.get_all("Delivery Note Item",
                         filters={"against_sales_invoice": si_name}, pluck="parent", distinct=True)
    live = frappe.get_all("Delivery Note",
                          filters={"name": ["in", fwd or [""]], "is_return": 0,
                                   "docstatus": ["<", 2]}, pluck="name")
    if len(live) > 1:
        problems.append(f"DUPLICATE DELIVERY NOTES: {live}")

    return problems


# ── scenario runner ──────────────────────────────────────────────────────────

OPS = ["adj_down", "adj_up", "return_part", "return_all", "cancel_return",
       "amend_return", "cancel_credit", "cancel_invoice"]


def run_scenario(ops, qty=QTY, rate=RATE, verbose=False):
    """Apply `ops` to a fresh sale and check invariants. Always rolls back."""
    _silence_slack()
    opened = flt(frappe.db.get_value("Bin", {"item_code": ITEM, "warehouse": WAREHOUSE}, "actual_qty"))
    log, blocked, problems = [], [], []
    si = dn = None
    last_return = None
    intended = flt(rate)
    last_intended = intended

    try:
        si, dn = new_sale(qty, rate)
        log.append(f"sale {si}/{dn} {qty} @ {rate}")

        for op in ops:
            sp = f"op{ops.index(op)}_{abs(hash(op)) % 9999}"
            frappe.db.savepoint(sp)
            try:
                last_intended = intended
                if op == "adj_down":
                    r = op_adjust(si, -50)
                    intended -= 50
                    log.append(f"adj_down -> {r['name']} (intended {intended:,.0f})")
                elif op == "adj_up":
                    r = op_adjust(si, +50)
                    intended += 50
                    log.append(f"adj_up -> {r['name']} (intended {intended:,.0f})")
                elif op == "return_part":
                    last_return = op_return(si, dn, max(1, qty // 4))
                    log.append(f"return_part -> {last_return}")
                elif op == "return_all":
                    last_return = op_return(si, dn, qty)
                    log.append(f"return_all -> {last_return}")
                elif op == "cancel_return":
                    if last_return:
                        op_cancel_return(last_return)
                        log.append(f"cancel_return {last_return}")
                elif op == "amend_return":
                    if last_return and frappe.db.get_value("Delivery Note", last_return, "docstatus") == 2:
                        last_return = op_amend_return(last_return)
                        log.append(f"amend_return -> {last_return}")
                elif op == "cancel_credit":
                    c = op_cancel_credit(si)
                    log.append(f"cancel_credit -> {c}")
                    if c:
                        # cancelling a correction puts its price change back
                        from fours_customizations.price_adjustment import effective_rates as _er
                        intended = flt(_er(si)[frappe.get_doc("Sales Invoice", si).items[0].name])
                elif op == "cancel_invoice":
                    op_cancel_invoice(si)
                    log.append("cancel_invoice")
                    intended = None
            except Exception as exc:
                frappe.db.rollback(save_point=sp)
                msg = " ".join(frappe.utils.strip_html(str(exc)).split())[:120]
                blocked.append(f"{op}: {type(exc).__name__}: {msg}")
                log.append(f"{op} BLOCKED (rolled back)")
                if op in ("adj_down", "adj_up"):
                    intended = last_intended

        problems = check(si, dn, opened, intended_rate=intended)
    except Exception:
        problems.append("HARNESS ERROR: " + traceback.format_exc().splitlines()[-1])
    finally:
        frappe.db.rollback()

    if verbose:
        for line in log:
            print("     " + line)
        for b in blocked:
            print("     blocked: " + b)
    return {"ops": ops, "problems": problems, "blocked": blocked, "log": log}


def crush(n=120, seed=7, length=(2, 4), verbose_failures=True):
    """Run n randomised permutations plus every ordered pair."""
    setup()
    random.seed(seed)

    scenarios = [list(p) for p in itertools.permutations(OPS, 2)]
    while len(scenarios) < n:
        k = random.randint(*length)
        scenarios.append([random.choice(OPS) for _ in range(k)])
    scenarios = scenarios[:n]

    failures, ran = [], 0
    for ops in scenarios:
        res = run_scenario(ops)
        ran += 1
        if res["problems"]:
            failures.append(res)

    print(f"\n{'=' * 78}")
    print(f"CRUSH: {ran} scenarios, {len(failures)} with invariant violations")
    print("=" * 78)

    seen = {}
    for f in failures:
        for p in f["problems"]:
            key = p.split(":")[0]
            seen.setdefault(key, []).append(f)
    for key, items in sorted(seen.items(), key=lambda kv: -len(kv[1])):
        print(f"\n### {key}  ({len(items)} scenarios)")
        ex = items[0]
        print(f"    example ops: {ex['ops']}")
        for p in ex["problems"]:
            print(f"      -> {p}")
        for line in ex["log"]:
            print(f"         {line}")
        for b in ex["blocked"]:
            print(f"         blocked: {b}")
    return failures
