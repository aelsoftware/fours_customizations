"""
payroll_print.py — "Print Payroll" button on Payroll Entry (PDF / Excel).

The button appears on the Payroll Entry form once every Salary Slip of the
entry has been cancelled — the point in this site's workflow where the final
figures are frozen and the physical payroll printout is produced. It offers
two outputs built from the same dataset:

  • Print PDF    — landscape A4, opened inline so the browser print dialog
                   can be used directly.
  • Export Excel — one-sheet .xlsx.

Both are styled with the 4S Industries brand colours taken from the logo on
multax.kit.africa: green #8FC643 on black #221E1F.

Columns:
  Employee, Designation, Basic, Sales Commission, Overtime Pay,
  Total Earnings, Late Deduction, No Checkout Deduction,
  Early Exit Deduction, Absent Deduction, Other Deductions,
  Total Deductions, Net Pay, Days Absent, Early Exit Days,
  No Checkout Days, Overtime Hours
"""

from __future__ import annotations

import base64
import mimetypes
import os
from io import BytesIO

import frappe
from frappe.utils import flt, getdate

from fours_customizations.fours_customizations.doctype.four_s_industries_settings.four_s_industries_settings import (
	get_setting,
)

BRAND_GREEN = "8FC643"
BRAND_BLACK = "221E1F"
BRAND_GREEN_TINT = "F1F8E3"  # light tint for zebra rows

# Overtime earning. The active structures use "Designation Overtime Pay"; the
# manual "Late Exit (Over Time)" earning is treated as overtime too so it shows
# in the Overtime Pay column rather than vanishing into the gross total only.
_OVERTIME_COMPONENTS = ("Designation Overtime Pay", "Late Exit (Over Time)")

# Each attendance-deduction column → the salary components that feed it. The
# active "4s Salary Structure with Deductions" (and the current slips) use the
# canonical "* Deduction" names; the legacy variants (Missed Checkout, Early
# Exit, Absence) are aliased so an older or alternate structure never leaks an
# attendance deduction into "Other Deductions". Alias groups are disjoint, so
# nothing is double-counted. Verified against multax.kit.africa: the June 2026
# run uses Absent / Late / Early Exit / No Checkout Deduction.
_ATTENDANCE_DEDUCTIONS = {
	"late_deduction": ("Late Deduction",),
	"no_checkout_deduction": ("No Checkout Deduction", "Missed Checkout"),
	"early_exit_deduction": ("Early Exit Deduction", "Early Exit"),
	"absent_deduction": ("Absent Deduction", "Absence"),
}

# (row key, column label, kind) — kind drives alignment & number format.
COLUMNS = [
	("employee", "Employee", "text"),
	("designation", "Designation", "text"),
	("basic", "Basic", "currency"),
	("sales_commission", "Sales Commission", "currency"),
	("overtime_pay", "Overtime Pay", "currency"),
	("total_earnings", "Total Earnings", "currency"),
	("late_deduction", "Late Deduction", "currency"),
	("no_checkout_deduction", "No Checkout Deduction", "currency"),
	("early_exit_deduction", "Early Exit Deduction", "currency"),
	("absent_deduction", "Absent Deduction", "currency"),
	("other_deductions", "Other Deductions", "currency"),
	("total_deductions", "Total Deductions", "currency"),
	("net_pay", "Net Pay", "currency"),
	("days_absent", "Days Absent", "int"),
	("early_exit_days", "Early Exit Days", "int"),
	("no_checkout_days", "No Checkout Days", "int"),
	("overtime_hours", "Overtime Hours", "hours"),
]


@frappe.whitelist()
def get_print_allowed(payroll_entry: str) -> dict:
	"""The form script asks this on refresh: the button shows in every state of
	the Payroll Entry (draft, submitted, cancelled) as long as it has at least
	one salary slip to print."""
	frappe.has_permission("Payroll Entry", "read", payroll_entry, throw=True)
	total = frappe.db.count("Salary Slip", {"payroll_entry": payroll_entry})
	return {"allowed": bool(total), "total": total}


@frappe.whitelist()
def download_payroll_excel(payroll_entry: str):
	pe = frappe.get_doc("Payroll Entry", payroll_entry)
	pe.check_permission("read")
	rows, totals = _get_payroll_rows(pe)

	frappe.local.response.filename = f"Payroll {pe.start_date} to {pe.end_date}.xlsx"
	frappe.local.response.filecontent = _build_excel(pe, rows, totals)
	frappe.local.response.type = "binary"


@frappe.whitelist()
def download_payroll_pdf(payroll_entry: str):
	from frappe.utils.pdf import get_pdf

	pe = frappe.get_doc("Payroll Entry", payroll_entry)
	pe.check_permission("read")
	rows, totals = _get_payroll_rows(pe)

	# type "pdf" serves the file inline, so the browser's print dialog works
	# straight from the new tab.
	frappe.local.response.filename = f"Payroll {pe.start_date} to {pe.end_date}.pdf"
	frappe.local.response.filecontent = get_pdf(_build_pdf_html(pe, rows, totals), {"orientation": "Landscape"})
	frappe.local.response.type = "pdf"


# ── dataset ─────────────────────────────────────────────────────────────────

def _get_payroll_rows(pe) -> tuple[list[dict], dict]:
	"""One row per employee of the Payroll Entry, plus a totals dict.

	The printout is available in every document state, so slips in any
	docstatus qualify — draft (0), submitted (1) and cancelled (2); when an
	employee has several (e.g. amended), the most recently created wins.
	"""
	slips = _latest_slips(pe.name)
	if not slips:
		frappe.throw("No salary slips found for this Payroll Entry.")

	components = _component_amounts([s.name for s in slips])
	commission_component = get_setting("commission_salary_component", "Sales Commission")

	rows = []
	for slip in slips:
		earnings = components.get(slip.name, {}).get("earnings", {})
		deductions = components.get(slip.name, {}).get("deductions", {})

		attendance = {
			key: sum(flt(deductions.get(name)) for name in names)
			for key, names in _ATTENDANCE_DEDUCTIONS.items()
		}
		# Everything on the slip that isn't one of the aliased attendance
		# deductions (loans, advances, shortages, …) is "Other Deductions".
		other_deductions = flt(slip.total_deduction) - sum(attendance.values())
		counts = _attendance_counts(slip.employee, slip.start_date, slip.end_date, pe.company)

		rows.append({
			"employee": slip.employee_name or slip.employee,
			"designation": slip.designation or "",
			"basic": _basic_amount(slip, earnings),
			"sales_commission": flt(earnings.get(commission_component)),
			"overtime_pay": sum(flt(earnings.get(c)) for c in _OVERTIME_COMPONENTS),
			"total_earnings": flt(slip.gross_pay),
			"late_deduction": attendance["late_deduction"],
			"no_checkout_deduction": attendance["no_checkout_deduction"],
			"early_exit_deduction": attendance["early_exit_deduction"],
			"absent_deduction": attendance["absent_deduction"],
			"other_deductions": flt(other_deductions, 2),
			"total_deductions": flt(slip.total_deduction),
			"net_pay": flt(slip.net_pay),
			"days_absent": counts["absent"],
			"early_exit_days": counts["early_exit"],
			"no_checkout_days": counts["no_checkout"],
			"overtime_hours": counts["overtime_hours"],
		})

	totals = {key: sum(r[key] for r in rows) for key, _label, kind in COLUMNS if kind != "text"}
	return rows, totals


def _latest_slips(payroll_entry: str) -> list:
	slips = frappe.get_all(
		"Salary Slip",
		filters={"payroll_entry": payroll_entry, "docstatus": ["in", [0, 1, 2]]},
		fields=[
			"name", "employee", "employee_name", "designation",
			"start_date", "end_date",
			"gross_pay", "total_deduction", "net_pay", "custom_basic_pay",
		],
		order_by="creation asc",
	)
	latest = {}
	for slip in slips:  # ascending creation → the newest slip per employee wins
		latest[slip.employee] = slip
	return sorted(latest.values(), key=lambda s: (s.employee_name or s.employee or ""))


def _component_amounts(slip_names: list[str]) -> dict:
	"""{slip name: {"earnings": {component: amount}, "deductions": {...}}}"""
	out: dict = {}
	if not slip_names:
		return out
	details = frappe.get_all(
		"Salary Detail",
		filters={"parent": ["in", slip_names], "parentfield": ["in", ["earnings", "deductions"]]},
		fields=["parent", "parentfield", "salary_component", "amount"],
	)
	for d in details:
		table = out.setdefault(d.parent, {"earnings": {}, "deductions": {}})[d.parentfield]
		table[d.salary_component] = table.get(d.salary_component, 0.0) + flt(d.amount)
	return out


def _basic_amount(slip, earnings: dict) -> float:
	"""The paid Basic earning; falls back to the stamped base when the slip's
	structure names its base component something other than "Basic"."""
	if "Basic" in earnings:
		return flt(earnings["Basic"])
	for component, amount in earnings.items():
		if "basic" in (component or "").lower():
			return flt(amount)
	return flt(slip.custom_basic_pay)


def _attendance_counts(employee, start_date, end_date, company) -> dict:
	"""Violation-day counts and overtime hours for the slip period, using the
	same shift-assignment + holiday gating the deductions themselves use."""
	counts = {"absent": 0, "early_exit": 0, "no_checkout": 0, "overtime_hours": 0.0}
	try:
		from fours_customizations.overtime_utils import calculate_designation_overtime
		from fours_customizations.salary_slip_handler import _holiday_dates, _shift_assigned_dates

		start, end = getdate(start_date), getdate(end_date)
		eligible = _shift_assigned_dates(employee, start, end) - _holiday_dates(
			employee, company, start, end
		)
		if eligible:
			records = frappe.get_all(
				"Attendance",
				filters={
					"employee": employee,
					"attendance_date": ["between", [start, end]],
					"docstatus": 1,
				},
				fields=["status", "attendance_date", "out_time", "early_exit"],
			)
			for att in records:
				if getdate(att.attendance_date) not in eligible:
					continue
				if att.status == "Absent":
					counts["absent"] += 1
				if att.early_exit == 1:
					counts["early_exit"] += 1
				if att.status in ("Present", "Half Day") and not att.out_time:
					counts["no_checkout"] += 1

		counts["overtime_hours"] = flt(
			calculate_designation_overtime(employee, start_date, end_date).get("total_hours")
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "4S Payroll Print: attendance counts failed")
	return counts


# ── Excel ───────────────────────────────────────────────────────────────────

_NUMBER_FORMATS = {"currency": "#,##0.00", "int": "0", "hours": "0.00"}


def _build_excel(pe, rows: list[dict], totals: dict) -> bytes:
	import openpyxl
	from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
	from openpyxl.utils import get_column_letter

	wb = openpyxl.Workbook()
	ws = wb.active
	ws.title = "Payroll"
	n_cols = len(COLUMNS)

	green_fill = PatternFill("solid", fgColor=BRAND_GREEN)
	black_fill = PatternFill("solid", fgColor=BRAND_BLACK)
	tint_fill = PatternFill("solid", fgColor=BRAND_GREEN_TINT)
	thin = Side(style="thin", color="D9D9D9")
	border = Border(left=thin, right=thin, top=thin, bottom=thin)

	ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=n_cols)
	title = ws.cell(row=1, column=1, value=pe.company)
	title.font = Font(bold=True, size=16, color="FFFFFF")
	title.fill = black_fill
	title.alignment = Alignment(horizontal="center", vertical="center")
	ws.row_dimensions[1].height = 28
	for col in range(2, n_cols + 1):
		ws.cell(row=1, column=col).fill = black_fill

	ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=n_cols)
	subtitle = ws.cell(
		row=2, column=1, value=f"Payroll — {pe.start_date} to {pe.end_date}  ({pe.name})"
	)
	subtitle.font = Font(bold=True, size=11, color=BRAND_BLACK)
	subtitle.fill = green_fill
	subtitle.alignment = Alignment(horizontal="center", vertical="center")
	ws.row_dimensions[2].height = 20
	for col in range(2, n_cols + 1):
		ws.cell(row=2, column=col).fill = green_fill

	header_row = 4
	for idx, (_key, label, _kind) in enumerate(COLUMNS, start=1):
		cell = ws.cell(row=header_row, column=idx, value=label)
		cell.font = Font(bold=True, color=BRAND_BLACK, size=9)
		cell.fill = green_fill
		cell.border = border
		cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
	ws.row_dimensions[header_row].height = 30
	ws.freeze_panes = f"A{header_row + 1}"

	for r_idx, row in enumerate(rows, start=header_row + 1):
		for c_idx, (key, _label, kind) in enumerate(COLUMNS, start=1):
			cell = ws.cell(row=r_idx, column=c_idx, value=row[key])
			cell.border = border
			cell.font = Font(size=9, color=BRAND_BLACK)
			if (r_idx - header_row) % 2 == 0:
				cell.fill = tint_fill
			if kind in _NUMBER_FORMATS:
				cell.number_format = _NUMBER_FORMATS[kind]
				cell.alignment = Alignment(horizontal="right")

	total_row = header_row + len(rows) + 1
	for c_idx, (key, _label, kind) in enumerate(COLUMNS, start=1):
		value = "TOTAL" if c_idx == 1 else totals.get(key, "")
		cell = ws.cell(row=total_row, column=c_idx, value=value)
		cell.font = Font(bold=True, color="FFFFFF", size=9)
		cell.fill = black_fill
		if kind in _NUMBER_FORMATS and value != "":
			cell.number_format = _NUMBER_FORMATS[kind]
			cell.alignment = Alignment(horizontal="right")

	for c_idx, (_key, label, kind) in enumerate(COLUMNS, start=1):
		width = 22 if kind == "text" else max(len(label) + 2, 12)
		ws.column_dimensions[get_column_letter(c_idx)].width = width

	buf = BytesIO()
	wb.save(buf)
	return buf.getvalue()


# ── PDF ─────────────────────────────────────────────────────────────────────

def _fmt(value, kind: str) -> str:
	if kind == "currency":
		return f"{flt(value):,.2f}"
	if kind == "int":
		return f"{int(value or 0)}"
	if kind == "hours":
		return f"{flt(value):,.2f}"
	return frappe.utils.escape_html(str(value or ""))


def _logo_data_uri() -> str | None:
	"""Company/site logo embedded as a data URI (wkhtmltopdf can't rely on
	fetching site URLs). Returns None when no local logo file is found."""
	candidates = [
		frappe.db.get_single_value("Website Settings", "app_logo"),
		frappe.db.get_single_value("Website Settings", "banner_image"),
		"/files/logo.png",
	]
	for logo in candidates:
		if not logo or logo.startswith("http"):
			continue
		relative = logo.lstrip("/")
		path = (
			frappe.get_site_path(relative)
			if relative.startswith("private/")
			else frappe.get_site_path("public", relative)
		)
		if os.path.exists(path):
			mime = mimetypes.guess_type(path)[0] or "image/png"
			with open(path, "rb") as f:
				return f"data:{mime};base64,{base64.b64encode(f.read()).decode()}"
	return None


def _build_pdf_html(pe, rows: list[dict], totals: dict) -> str:
	logo = _logo_data_uri()
	logo_html = f'<img src="{logo}" style="height:52px;">' if logo else ""

	header_cells = "".join(f"<th>{label}</th>" for _key, label, _kind in COLUMNS)

	body_rows = []
	for row in rows:
		cells = "".join(
			f'<td class="{ "num" if kind != "text" else "" }">{_fmt(row[key], kind)}</td>'
			for key, _label, kind in COLUMNS
		)
		body_rows.append(f"<tr>{cells}</tr>")

	total_cells = ["<td>TOTAL</td>"]
	for key, _label, kind in COLUMNS[1:]:
		total_cells.append(
			f'<td class="num">{_fmt(totals[key], kind)}</td>' if kind != "text" else "<td></td>"
		)

	return f"""
<style>
	body {{ font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; color: #{BRAND_BLACK}; margin: 0; }}
	.band {{ border-bottom: 4px solid #{BRAND_GREEN}; padding: 6px 0 10px 0; }}
	.band table {{ width: 100%; border: none; }}
	.band td {{ border: none; vertical-align: middle; }}
	.company {{ font-size: 18px; font-weight: bold; }}
	.meta {{ font-size: 10px; color: #555; }}
	.title {{ font-size: 13px; font-weight: bold; color: #{BRAND_GREEN}; text-align: right;
		text-transform: uppercase; letter-spacing: 2px; }}
	.period {{ font-size: 10px; text-align: right; color: #{BRAND_BLACK}; }}
	table.payroll {{ width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 8px; }}
	table.payroll th {{ background: #{BRAND_GREEN}; color: #{BRAND_BLACK}; font-weight: bold;
		padding: 5px 4px; border: 1px solid #{BRAND_GREEN}; text-align: center; }}
	table.payroll td {{ border: 1px solid #DDDDDD; padding: 4px; }}
	table.payroll tbody tr:nth-child(even) td {{ background: #{BRAND_GREEN_TINT}; }}
	td.num {{ text-align: right; white-space: nowrap; }}
	tr.totals td {{ background: #{BRAND_BLACK}; color: #FFFFFF; font-weight: bold;
		border-color: #{BRAND_BLACK}; }}
</style>
<div class="band">
	<table>
		<tr>
			<td style="width:60px;">{logo_html}</td>
			<td>
				<div class="company">{frappe.utils.escape_html(pe.company or "")}</div>
				<div class="meta">{frappe.utils.escape_html(pe.name)}</div>
			</td>
			<td>
				<div class="title">Payroll</div>
				<div class="period">{pe.start_date} to {pe.end_date}</div>
			</td>
		</tr>
	</table>
</div>
<table class="payroll">
	<thead><tr>{header_cells}</tr></thead>
	<tbody>
		{"".join(body_rows)}
		<tr class="totals">{"".join(total_cells)}</tr>
	</tbody>
</table>
"""
