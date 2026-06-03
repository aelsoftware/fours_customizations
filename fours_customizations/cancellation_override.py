"""
Cancellation Override — Fours Customizations
============================================

Overrides ``cancellation_requests.api.request_cancellation`` so that a
cancellation request raised on a **Sales Invoice whose Delivery Note is still
in draft** is fast-tracked instead of waiting for a manual approver:

  1. The draft Delivery Note(s) auto-created from the invoice are deleted.
  2. The Sales Invoice is cancelled automatically.
  3. The auto-created Sales Order (and its Stock Reservation Entries) is torn
     down via the existing Delivery Note ``on_trash`` chain.
  4. A notification is sent to every user with cancel permission on Sales
     Invoice ("it has been cancelled and the reason was …") and to the
     requester ("your invoice has been auto-cancelled").

Anything else — a different doctype, a return invoice, or a Sales Invoice whose
Delivery Note has already been **submitted** (goods are out the door) — falls
through unchanged to the original ``cancellation_requests`` manual-approval
workflow.

The override is wired up through ``override_whitelisted_methods`` in
``hooks.py``; the client (cancellation_button.js) keeps calling the original
method path, so no front-end change is required.
"""

import frappe
from frappe import _
from frappe.utils import escape_html, get_fullname

from cancellation_requests.utils import (
    build_document_link,
    create_notification_log,
    get_doctype_config,
    get_settings,
    post_to_slack_webhook,
    resolve_cancellation_recipients,
    send_slack_dm,
)

INVOICE_DOCTYPE = "Sales Invoice"


@frappe.whitelist()
def request_cancellation(doctype, name, reason):
    """Drop-in replacement for ``cancellation_requests.api.request_cancellation``.

    Fast-tracks eligible Sales Invoices; everything else is delegated, unchanged,
    to the original implementation (imported directly so we never recurse back
    through the override map).
    """
    if doctype == INVOICE_DOCTYPE:
        result = _try_auto_cancel_invoice(name, reason)
        if result is not None:
            return result

    from cancellation_requests.api import request_cancellation as _original

    return _original(doctype, name, reason)


# ── eligibility + orchestration ────────────────────────────────────────────────


def _try_auto_cancel_invoice(name, reason):
    """Return a response dict when the invoice was auto-cancelled, else ``None``.

    ``None`` is the signal to the caller to defer to the standard request flow.
    """
    if not name:
        return None

    # Only act when Sales Invoice is actually configured for cancellation
    # requests; otherwise let the original method raise its own clear error.
    config = get_doctype_config(INVOICE_DOCTYPE)
    if not config or not config.get("enabled"):
        return None

    if not reason or not reason.strip():
        frappe.throw(_("Reason is required"))

    si = frappe.get_doc(INVOICE_DOCTYPE, name)

    # Only submitted, forward (non-return) invoices are eligible.
    if si.docstatus != 1 or si.get("is_return"):
        return None

    draft_dns = _get_draft_delivery_notes(si.name)
    if draft_dns is None:
        # A submitted Delivery Note exists — goods already delivered; this must
        # go through the manual approval path.
        return None
    if not draft_dns:
        # No Delivery Note at all — defer to the manual approval flow.
        return None

    requester = frappe.session.user
    reason = reason.strip()

    _perform_auto_cancellation(si, draft_dns, reason)

    # Notifications must never roll back the cancellation we just committed.
    try:
        _notify(si, reason, requester)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "4S Auto-Cancel: notification failed")

    return {
        "message": _(
            "Sales Invoice {0} was automatically cancelled and its draft "
            "Delivery Note removed."
        ).format(si.name)
    }


def _get_draft_delivery_notes(invoice_name):
    """Resolve the forward Delivery Notes linked to this invoice.

    Returns:
        * ``None``  — a *submitted* Delivery Note exists, so the fast-track must
          be aborted (caller defers to manual approval).
        * ``list``  — the names of the *draft* Delivery Notes (possibly empty).
    """
    dn_names = frappe.get_all(
        "Delivery Note Item",
        filters={"against_sales_invoice": invoice_name},
        pluck="parent",
        distinct=True,
    )
    if not dn_names:
        return []

    rows = frappe.get_all(
        "Delivery Note",
        filters={"name": ["in", dn_names], "is_return": 0},
        fields=["name", "docstatus"],
    )
    if any(r.docstatus == 1 for r in rows):
        return None
    return [r.name for r in rows if r.docstatus == 0]


def _perform_auto_cancellation(si, draft_dns, reason):
    """Stamp the reason, cancel the invoice, drop the draft DN(s), tear down SOs.

    The whole block runs with ``frappe.flags.ignore_permissions`` elevated: the
    requester deliberately lacks cancel rights (that is why they are *requesting*
    cancellation), yet this system-initiated action must cancel the invoice, the
    auto-created Sales Order, and its Stock Reservation Entries on their behalf.

    Order of operations:
      1. Cancel the invoice while the Sales Order is still live, so ERPNext can
         cleanly reset the order's billing status.
      2. Delete the draft Delivery Note(s) — ``delivery_note_handler.on_trash``
         then cancels the linked Sales Order + Stock Reservation Entries.
      3. Safety net for invoices created before native DN→SO linking existed:
         explicitly tear down any Sales Order still linked to the invoice.
    """
    # Record the reason on the (read-only, shown-when-cancelled) standard field,
    # then reload so the in-memory invoice matches the DB. cancel() calls save(),
    # which would otherwise overwrite the reason — and a reload keeps the value
    # unchanged relative to the DB so the after-submit field guard stays happy.
    if si.meta.has_field("cancellation_reason"):
        frappe.db.set_value(
            INVOICE_DOCTYPE, si.name, "cancellation_reason", reason, update_modified=False
        )
        si.reload()

    saved_flag = frappe.flags.ignore_permissions
    frappe.flags.ignore_permissions = True
    try:
        # Flags are set after the reload above, which would otherwise reset them.
        si.flags.ignore_permissions = True
        si.flags.ignore_links = True
        si.cancel()

        for dn_name in draft_dns:
            frappe.delete_doc("Delivery Note", dn_name, ignore_permissions=True)

        _teardown_linked_sales_orders(si)
    finally:
        frappe.flags.ignore_permissions = saved_flag


def _teardown_linked_sales_orders(si):
    """Cancel any Sales Order still linked to this invoice (and its dependants).

    For invoices created after native linking, the Delivery Note deletion above
    already cancelled the order, so the chain short-circuits on ``docstatus == 2``
    and this is a no-op. For older invoices (DN not linked to the SO) it performs
    the teardown. ``deleted_dn=""`` is a sentinel that matches no real document,
    so the chain's "other submitted Delivery Notes" guard still protects orders
    that have genuinely delivered notes.
    """
    from fours_customizations.delivery_note_handler import _cancel_sales_order_chain

    so_names = {item.sales_order for item in si.items if item.get("sales_order")}
    # Also catch auto-created orders via their back-pointer — covers invoices
    # whose items predate native sales_order linking.
    so_names.update(
        frappe.get_all(
            "Sales Order",
            filters={"custom_source_sales_invoice": si.name},
            pluck="name",
        )
    )
    so_names.discard(None)

    failed = []
    for so_name in so_names:
        try:
            _cancel_sales_order_chain(so_name, deleted_dn="")
        except Exception:
            failed.append(so_name)
            frappe.log_error(frappe.get_traceback(), "4S Auto-Cancel: SO teardown failed")

    # The invoice cancellation already committed; if an order couldn't follow it
    # down, say so loudly rather than leaving a live order behind silently.
    if failed:
        frappe.msgprint(
            _(
                "Sales Invoice was cancelled, but Sales Order(s) {0} could not be "
                "cancelled automatically — please cancel them manually."
            ).format(", ".join(failed)),
            indicator="orange",
            alert=True,
        )


# ── notifications ───────────────────────────────────────────────────────────────


def _notify(si, reason, requester):
    """Notify cancel-permission users and the requester of the auto-cancellation."""
    settings = None
    try:
        settings = get_settings()
    except Exception:
        pass
    enable_email = bool(getattr(settings, "enable_email", 1)) if settings else True
    enable_slack = bool(getattr(settings, "enable_slack", 1)) if settings else True

    requester_name = get_fullname(requester)
    link_html = build_document_link(si, "html")
    link_slack = build_document_link(si, "slack")
    safe_reason = escape_html(reason)
    config = get_doctype_config(INVOICE_DOCTYPE) or {}

    # 1. Everyone with cancel permission on Sales Invoice (company-scoped, the
    #    same audience the manual request flow targets), minus the requester.
    cancellers = resolve_cancellation_recipients(si, exclude_user=requester)

    canceller_subject = _("Sales Invoice {0} auto-cancelled").format(si.name)
    canceller_html = (
        f"<p>Sales Invoice {link_html} has been <b>automatically cancelled</b> "
        f"because a cancellation was requested while its Delivery Note was still "
        f"in draft.</p>"
        f"<p><b>Requested by:</b> {escape_html(requester_name)}</p>"
        f"<p><b>Reason:</b> {safe_reason}</p>"
    )
    canceller_slack = (
        f"*Sales Invoice auto-cancelled*\n"
        f"*Invoice:* {link_slack}\n"
        f"*Requested by:* {requester_name}\n"
        f"*Reason:* {reason}"
    )

    if enable_email and cancellers:
        _safe_sendmail(cancellers, canceller_subject, canceller_html, si)
    if enable_slack and config.get("slack_webhook_url"):
        post_to_slack_webhook(config["slack_webhook_url"], canceller_slack)
    for user in cancellers:
        create_notification_log(user, canceller_subject, si, canceller_html, from_user=requester)
        if enable_slack:
            send_slack_dm(user, canceller_slack)

    # 2. The requester — confirm their request was auto-processed.
    if requester and requester not in ("Administrator", "Guest"):
        req_subject = _(
            "Your cancellation request for Sales Invoice {0} was auto-processed"
        ).format(si.name)
        req_html = (
            f"<p>Your cancellation request has been processed automatically.</p>"
            f"<p>Sales Invoice {link_html} has been <b>cancelled</b> because its "
            f"Delivery Note was still in draft.</p>"
            f"<p><b>Reason:</b> {safe_reason}</p>"
        )
        req_slack = (
            f"Your cancellation request has been auto-processed.\n"
            f"*Sales Invoice:* {link_slack} has been cancelled.\n"
            f"*Reason:* {reason}"
        )
        if enable_email:
            _safe_sendmail([requester], req_subject, req_html, si)
        create_notification_log(requester, req_subject, si, req_html, from_user=requester)
        if enable_slack:
            send_slack_dm(requester, req_slack)


def _safe_sendmail(recipients, subject, message, si):
    recipients = [r for r in recipients if r and r not in ("Administrator", "Guest")]
    if not recipients:
        return
    try:
        frappe.sendmail(
            recipients=recipients,
            subject=subject,
            message=message,
            reference_doctype=si.doctype,
            reference_name=si.name,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "4S Auto-Cancel: email failed")
