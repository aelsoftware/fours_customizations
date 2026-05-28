"""
notifications.py — Shared email + Slack notification helpers.

All notification recipients, times, and slack credentials live in the
"Four S Industries Settings" single doctype.  Each helper here reads the relevant
fields lazily so the settings can be updated without restarting the worker.
"""

from __future__ import annotations

import json
from typing import Iterable

import frappe
from frappe.utils import cstr

from fours_customizations.fours_customizations.doctype.four_s_industries_settings.four_s_industries_settings import (
	get_settings,
)


def _split_csv(raw: str | None) -> list[str]:
	if not raw:
		return []
	return [part.strip() for part in cstr(raw).split(",") if part.strip()]


def send_email(subject: str, message: str, recipients, cc=None, attachments=None) -> None:
	"""Send an email via Frappe's mail queue, silently swallowing failures.

	Failures are logged so we never crash a scheduled job over a notification.
	"""
	to = recipients if isinstance(recipients, list) else _split_csv(recipients)
	cc_list = cc if isinstance(cc, list) else _split_csv(cc)
	to = [addr for addr in to if addr]
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
