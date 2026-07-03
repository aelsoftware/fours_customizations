"""
notifications.py — Shared email + Slack notification helpers.

All notification recipients, times, and slack credentials live in the
"Four S Industries Settings" single doctype.  Each helper here reads the relevant
fields lazily so the settings can be updated without restarting the worker.
"""

from __future__ import annotations

import json
import re
from typing import Iterable

import frappe
from frappe.utils import cstr, validate_email_address

from fours_customizations.fours_customizations.doctype.four_s_industries_settings.four_s_industries_settings import (
	get_settings,
)


def _split_addresses(raw) -> list[str]:
	"""Split an address list on commas, semicolons, and newlines."""
	if not raw:
		return []
	if isinstance(raw, (list, tuple)):
		parts = []
		for entry in raw:
			parts.extend(_split_addresses(entry))
		return parts
	return [part.strip() for part in re.split(r"[,;\n]", cstr(raw)) if part.strip()]


def _clean_addresses(raw, label: str) -> list[str]:
	"""Split, validate, and dedupe an address list.

	frappe.sendmail silently drops invalid recipients — and when every main
	recipient is dropped it still delivers to the CC list, which is exactly the
	"only CC got the email" bug. So invalid addresses are surfaced in the Error
	Log here instead of vanishing quietly.
	"""
	valid, invalid, seen = [], [], set()
	for addr in _split_addresses(raw):
		parsed = validate_email_address(addr)
		if parsed:
			key = parsed.lower()
			if key not in seen:
				seen.add(key)
				valid.append(parsed)
		else:
			invalid.append(addr)
	if invalid:
		frappe.log_error(
			f"Invalid {label} email address(es) skipped: {', '.join(invalid)}. "
			"Fix them in Four S Industries Settings.",
			"4S Notifications: invalid recipient",
		)
	return valid


def send_email(subject: str, message: str, recipients, cc=None, attachments=None) -> None:
	"""Send an email via Frappe's mail queue, silently swallowing failures.

	Failures are logged so we never crash a scheduled job over a notification.
	"""
	to = _clean_addresses(recipients, "recipient")
	cc_list = [addr for addr in _clean_addresses(cc, "CC") if addr not in to]
	if not to:
		# Never send a CC-only email — promote the CC list so someone always
		# receives it as a direct recipient.
		to, cc_list = cc_list, []
	if not to:
		return
	try:
		frappe.sendmail(
			recipients=to,
			cc=cc_list or None,
			subject=subject,
			message=message,
			attachments=attachments or None,
			delayed=False,
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "4S Notifications: email failed")


def send_slack(message: str, attachments: Iterable[dict] | None = None) -> None:
	"""Post `message` to Slack via the configured incoming webhook.

	No-ops if the webhook URL is empty.  Failures are logged, not raised.
	"""
	settings = get_settings()
	webhook = settings.get_password("slack_webhook_url", raise_exception=False) if settings else None
	if not webhook:
		return

	payload: dict = {"text": message}
	if settings.slack_channel:
		payload["channel"] = settings.slack_channel
	if attachments:
		payload["attachments"] = list(attachments)

	try:
		import requests

		requests.post(webhook, data=json.dumps(payload), headers={"Content-Type": "application/json"}, timeout=10)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "4S Notifications: slack failed")
