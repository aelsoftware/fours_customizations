"""
payroll_handler.py — Monthly Payroll Entry creation + email export (Req #3).

`create_monthly_payroll_entry()` runs every day at midnight; it fires only
on the configured Payroll Day of Month (default 30, or the last day of the
month if the month is shorter).

The Payroll Entry is created, salary slips queued, and a one-sheet Excel
file is built and attached to an email sent to the boss.
"""

from __future__ import annotations

from datetime import date
from io import BytesIO

import frappe
from frappe.utils import (
	add_months,
	flt,
	get_first_day,
	get_last_day,
	getdate,
	now_datetime,
)

from fours_customizations.fours_customizations.doctype.four_s_industries_settings.four_s_industries_settings import (
	get_settings,
)
from fours_customizations.notifications import send_email


_OFFSET_DAYS = {
	"1st day of next month": 1,
	"2nd day of next month": 2,
	"3rd day of next month": 3,
	"4th day of next month": 4,
}


def _payroll_period(settings, run_date: date) -> tuple[date, date]:
	"""Given a *run_date* (i.e. today, the day the dispatcher fires), return
	the (start, end) of the salary period this Payroll Entry should cover.

	    Run day                                Period covered
	    "Last day of payroll period month"     Same month as run_date
	    "Nth day of next month"                Previous month
	"""
	option = (settings.payroll_day_of_month or "1st day of next month").strip()
	if option == "Last day of payroll period month":
		return get_first_day(run_date), get_last_day(run_date)
	prev = add_months(run_date, -1)
	return get_first_day(prev), get_last_day(prev)


def _should_run_today(settings, today: date) -> bool:
	option = (settings.payroll_day_of_month or "1st day of next month").strip()

	if option == "Last day of payroll period month":
		return today == get_last_day(today)

	wanted_day = _OFFSET_DAYS.get(option, 1)
	return today.day == wanted_day


def daily_payroll_dispatcher() -> dict:
	settings = get_settings()
	if not int(settings.enable_payroll_automation or 0):
		return {"skipped": True}

	today = getdate(now_datetime())
	if not _should_run_today(settings, today):
		return {"skipped": True, "reason": "not the day", "today": str(today)}

	start, end = _payroll_period(settings, today)
	key = f"4s_payroll:{start.year}-{start.month}"
	if frappe.cache().get_value(key):
		return {"skipped": True, "reason": "already ran"}

	result = create_monthly_payroll_entry(settings, today, start, end)
	frappe.cache().set_value(key, "1", expires_in_sec=60 * 60 * 24 * 7)
	return result


def create_monthly_payroll_entry(settings=None, run_date=None, start=None, end=None) -> dict:
	if settings is None:
		settings = get_settings()
	if run_date is None:
		run_date = getdate(now_datetime())
	if start is None or end is None:
		start, end = _payroll_period(settings, run_date)

	company = settings.default_payroll_company or settings.default_company
	if not company:
		frappe.log_error("4S Payroll: no company configured", "4S Payroll")
		return {"error": "no company"}

	if frappe.db.exists(
		"Payroll Entry",
		{"company": company, "start_date": start, "end_date": end, "docstatus": ("!=", 2)},
	):
		return {"skipped": True, "reason": "payroll entry exists"}

	pe = frappe.new_doc("Payroll Entry")
	pe.company = company
	pe.posting_date = run_date
	pe.payroll_frequency = "Monthly"
	pe.start_date = start
	pe.end_date = end
	pe.exchange_rate = 1
	pe.flags.ignore_permissions = True
	try:
		pe.fill_employee_details()
	except Exception:
		# Older Frappe versions
		try:
			pe.get_emp_list()
		except Exception:
			frappe.log_error(frappe.get_traceback(), "4S Payroll: fill employees failed")

	pe.insert(ignore_permissions=True)

	# Try to create salary slips. Failures here shouldn't kill the email step.
	try:
		pe.submit()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "4S Payroll: submit failed (continuing)")

	excel = build_payroll_excel(pe.name)
	_send_payroll_email(settings, pe, excel)

	return {"created": pe.name}


def build_payroll_excel(payroll_entry: str) -> bytes:
	"""Build a one-sheet Excel file with one row per Salary Slip in the run."""
	import openpyxl

	pe = frappe.get_doc("Payroll Entry", payroll_entry)
	slips = frappe.get_all(
		"Salary Slip",
		filters={"payroll_entry": payroll_entry},
		fields=[
			"name", "employee", "employee_name", "department", "designation",
			"gross_pay", "total_deduction", "net_pay", "bank_account_no", "bank_name",
		],
		order_by="employee_name",
	)

	wb = openpyxl.Workbook()
	ws = wb.active
	ws.title = f"Payroll {pe.end_date}"

	headers = [
		"Employee", "Employee Name", "Department", "Designation",
		"Bank", "Account No.",
		"Gross Pay", "Total Deductions", "Net Pay",
	]
	ws.append(headers)

	for h in ws[1]:
		h.font = openpyxl.styles.Font(bold=True)

	total_gross = total_deductions = total_net = 0.0

	for slip in slips:
		ws.append([
			slip.employee,
			slip.employee_name,
			slip.department or "",
			slip.designation or "",
			slip.bank_name or "",
			slip.bank_account_no or "",
			flt(slip.gross_pay),
			flt(slip.total_deduction),
			flt(slip.net_pay),
		])
		total_gross += flt(slip.gross_pay)
		total_deductions += flt(slip.total_deduction)
		total_net += flt(slip.net_pay)

	ws.append([])
	ws.append(["TOTAL", "", "", "", "", "", total_gross, total_deductions, total_net])
	for c in ws[ws.max_row]:
		c.font = openpyxl.styles.Font(bold=True)

	# Autosize columns
	for column in ws.columns:
		length = max((len(str(cell.value)) for cell in column if cell.value is not None), default=10)
		col_letter = column[0].column_letter
		ws.column_dimensions[col_letter].width = min(length + 2, 40)

	buf = BytesIO()
	wb.save(buf)
	return buf.getvalue()


def _send_payroll_email(settings, pe, excel_bytes: bytes) -> None:
	to = settings.boss_email
	if not to:
		frappe.log_error("4S Payroll: boss_email not configured", "4S Payroll")
		return
	subject = f"Payroll Run — {pe.end_date}"
	body = f"""
<p>Dear Sir / Madam,</p>
<p>Attached is the payroll run for the month ending <b>{pe.end_date}</b>.</p>
<ul>
  <li>Payroll Entry: <b>{pe.name}</b></li>
  <li>Start: {pe.start_date}</li>
  <li>End: {pe.end_date}</li>
</ul>
<p>Please review and approve in ERPNext.</p>
"""
	filename = f"payroll-{pe.end_date}.xlsx"
	send_email(
		subject=subject,
		message=body,
		recipients=to,
		cc=settings.payroll_cc_emails,
		attachments=[{"fname": filename, "fcontent": excel_bytes}],
	)
