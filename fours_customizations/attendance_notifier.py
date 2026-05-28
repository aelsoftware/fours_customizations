"""
attendance_notifier.py — Daily summary of late comers + absentees (Req #6).

Reads "today's" Attendance, builds an HTML email + a Slack message, and
sends them to the addresses configured in Fours Industries Settings.

The cron runs hourly; the actual send is gated by `attendance_notification_time`
so the recipient receives exactly one daily report.
"""

from __future__ import annotations

import frappe
from frappe.utils import format_time, get_time, getdate, now_datetime

from fours_customizations.fours_customizations.doctype.four_s_industries_settings.four_s_industries_settings import (
	get_settings,
)
from fours_customizations.notifications import send_email, send_slack


def _format_html(date_str, lates, absentees) -> str:
	def _row(rows, fields):
		body = []
		for r in rows:
			cells = "".join(
				f"<td style='padding:6px 10px;border:1px solid #ddd;'>{r.get(f) or ''}</td>"
				for f in fields
			)
			body.append(f"<tr>{cells}</tr>")
		return "".join(body)

	return f"""
<div style="font-family:'Segoe UI',Arial,sans-serif;line-height:1.6;color:#222;">
  <h3 style="margin-bottom:6px;">Daily Attendance Summary — {date_str}</h3>

  <h4 style="margin-top:18px;color:#c0392b;">Absentees ({len(absentees)})</h4>
  <table style="border-collapse:collapse;width:100%;font-size:13px;">
    <thead><tr style="background:#f0f0f0;">
      <th style="padding:6px 10px;border:1px solid #ddd;text-align:left;">Employee</th>
      <th style="padding:6px 10px;border:1px solid #ddd;text-align:left;">Name</th>
      <th style="padding:6px 10px;border:1px solid #ddd;text-align:left;">Department</th>
    </tr></thead>
    <tbody>{_row(absentees, ['employee', 'employee_name', 'department']) or '<tr><td colspan=3 style="padding:6px 10px;border:1px solid #ddd;color:#666;">No absentees today.</td></tr>'}</tbody>
  </table>

  <h4 style="margin-top:18px;color:#d68910;">Late Arrivals ({len(lates)})</h4>
  <table style="border-collapse:collapse;width:100%;font-size:13px;">
    <thead><tr style="background:#f0f0f0;">
      <th style="padding:6px 10px;border:1px solid #ddd;text-align:left;">Employee</th>
      <th style="padding:6px 10px;border:1px solid #ddd;text-align:left;">Name</th>
      <th style="padding:6px 10px;border:1px solid #ddd;text-align:left;">In Time</th>
    </tr></thead>
    <tbody>{_row(lates, ['employee', 'employee_name', 'in_time']) or '<tr><td colspan=3 style="padding:6px 10px;border:1px solid #ddd;color:#666;">No late arrivals today.</td></tr>'}</tbody>
  </table>
</div>
"""


def _format_slack(date_str, lates, absentees) -> str:
	lines = [f"*Daily Attendance — {date_str}*"]
	lines.append(f"• Absent: {len(absentees)}")
	for a in absentees[:15]:
		lines.append(f"   – {a.get('employee_name') or a.get('employee')}")
	if len(absentees) > 15:
		lines.append(f"   …and {len(absentees) - 15} more")

	lines.append(f"• Late: {len(lates)}")
	for la in lates[:15]:
		in_t = la.get("in_time")
		if in_t:
			in_t = format_time(in_t)
		lines.append(f"   – {la.get('employee_name') or la.get('employee')} ({in_t or 'n/a'})")
	if len(lates) > 15:
		lines.append(f"   …and {len(lates) - 15} more")
	return "\n".join(lines)


def send_daily_attendance_summary(target_date: str | None = None) -> dict:
	"""Send the daily summary to the configured recipients."""
	settings = get_settings()
	day = getdate(target_date) if target_date else getdate(now_datetime())

	lates = frappe.get_all(
		"Attendance",
		filters={"attendance_date": day, "docstatus": 1, "late_entry": 1},
		fields=["employee", "employee_name", "department", "in_time"],
	)
	absentees = frappe.get_all(
		"Attendance",
		filters={"attendance_date": day, "docstatus": 1, "status": "Absent"},
		fields=["employee", "employee_name", "department"],
	)

	if not lates and not absentees:
		return {"sent": False, "reason": "no events"}

	html = _format_html(str(day), lates, absentees)
	slack_text = _format_slack(str(day), lates, absentees)

	recipients = settings.attendance_notification_recipient
	cc = settings.attendance_notification_cc
	if recipients:
		send_email(
			subject=f"Attendance Summary — {day}",
			message=html,
			recipients=recipients,
			cc=cc,
		)
	send_slack(slack_text)
	return {"sent": True, "lates": len(lates), "absentees": len(absentees)}


def hourly_attendance_notifier():
	"""Hourly cron gate that fires the actual notifier at the configured hour."""
	settings = get_settings()
	if not int(settings.enable_attendance_automation or 0):
		return
	target = get_time(settings.attendance_notification_time or "09:30:00")
	now = now_datetime()
	if now.hour != target.hour:
		return
	# Marker key avoids double-sending if the hourly tick fires twice
	mark_key = f"4s_attendance_notifier:{now.date()}"
	if frappe.cache().get_value(mark_key):
		return
	send_daily_attendance_summary()
	frappe.cache().set_value(mark_key, "1", expires_in_sec=2 * 60 * 60)
