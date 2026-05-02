"""
Salary Slip Handler for Fours Customizations
Automatically calculates attendance-based deductions and overtime
"""

import frappe
from frappe import _


def calculate_and_add_deductions(doc, method=None):
	"""
	Calculate attendance-based deductions and add them to salary slip.
	This function should be called via Server Script or doc_events hook.

	Works with both:
	- Manual salary slip creation
	- Bulk creation via Payroll Entry

	Usage in Server Script (Salary Slip - Before Insert AND Before Save):
		from fours_customizations.salary_slip_handler import calculate_and_add_deductions
		calculate_and_add_deductions(doc)
	"""

	# Only process draft salary slips
	if doc.docstatus != 0:
		return

	if not doc.employee or not doc.start_date or not doc.end_date:
		return

	# Don't run if salary structure hasn't been loaded yet
	# (earnings should have at least one component from the structure)
	if not doc.earnings or len(doc.earnings) == 0:
		return

	# Check if we've already processed this slip (to avoid duplicate processing)
	if hasattr(doc, '_deductions_calculated'):
		return

	# Mark as processed
	doc._deductions_calculated = True

	try:
		# Get employee designation
		employee = frappe.get_doc('Employee', doc.employee)

		if not employee.designation:
			return

		# Get designation with deduction rates
		designation = frappe.get_doc('Designation', employee.designation)
	except Exception as e:
		frappe.log_error(f"Error loading employee/designation: {str(e)}", "Salary Slip Handler")
		return

	# Get attendance records for the period
	attendance_records = frappe.get_all(
		'Attendance',
		filters={
			'employee': doc.employee,
			'attendance_date': ['between', [doc.start_date, doc.end_date]],
			'docstatus': 1  # Only submitted attendance
		},
		fields=['name', 'status', 'in_time', 'out_time', 'late_entry', 'early_exit']
	)

	# Count violations
	absent_count = 0
	late_count = 0
	early_exit_count = 0
	no_checkout_count = 0

	for att in attendance_records:
		# Count absences
		if att.status == 'Absent':
			absent_count += 1

		# Count late entries
		if att.late_entry == 1:
			late_count += 1

		# Count early exits
		if att.early_exit == 1:
			early_exit_count += 1

		# Count no checkout (present but no out_time)
		if att.status in ['Present', 'Half Day'] and not att.out_time:
			no_checkout_count += 1

	# Calculate deduction amounts
	deductions_map = {
		'Absent Deduction': {
			'count': absent_count,
			'rate': designation.absent_deduction or 0,
			'amount': absent_count * (designation.absent_deduction or 0)
		},
		'Late Deduction': {
			'count': late_count,
			'rate': designation.late_deduction or 0,
			'amount': late_count * (designation.late_deduction or 0)
		},
		'Early Exit Deduction': {
			'count': early_exit_count,
			'rate': designation.early_exit_deduction or 0,
			'amount': early_exit_count * (designation.early_exit_deduction or 0)
		},
		'No Checkout Deduction': {
			'count': no_checkout_count,
			'rate': designation.no_checkout_deduction or 0,
			'amount': no_checkout_count * (designation.no_checkout_deduction or 0)
		}
	}

	# Add or update deduction components in salary slip
	for component_name, deduction_data in deductions_map.items():
		if deduction_data['amount'] > 0:
			# Check if component already exists
			existing_component = None
			for ded in doc.deductions:
				if ded.salary_component == component_name:
					existing_component = ded
					break

			if existing_component:
				# Update existing
				existing_component.amount = deduction_data['amount']
			else:
				# Add new
				doc.append('deductions', {
					'salary_component': component_name,
					'amount': deduction_data['amount']
				})

	# Calculate and add overtime if configured
	if hasattr(designation, 'overtime_start_time') and designation.overtime_start_time:
		from fours_customizations.overtime_utils import calculate_designation_overtime

		overtime_data = calculate_designation_overtime(
			doc.employee,
			doc.start_date,
			doc.end_date
		)

		if overtime_data['total_amount'] > 0:
			# Check if overtime component already exists
			existing_overtime = None
			for earning in doc.earnings:
				if earning.salary_component == 'Designation Overtime Pay':
					existing_overtime = earning
					break

			if existing_overtime:
				existing_overtime.amount = overtime_data['total_amount']
			else:
				doc.append('earnings', {
					'salary_component': 'Designation Overtime Pay',
					'amount': overtime_data['total_amount']
				})

	# Recalculate totals
	doc.gross_pay = sum([e.amount for e in doc.earnings])
	doc.total_deduction = sum([d.amount for d in doc.deductions])
	doc.net_pay = doc.gross_pay - doc.total_deduction

	# Log what was calculated (for debugging)
	frappe.logger().info(f"Calculated deductions for {doc.employee}: {deductions_map}")


def get_attendance_summary(employee, start_date, end_date):
	"""
	Get a summary of attendance violations for an employee in a period.
	Useful for displaying in salary slip or reports.

	Returns:
		dict: Summary of violations and amounts
	"""
	employee_doc = frappe.get_doc('Employee', employee)

	if not employee_doc.designation:
		return {'error': 'Employee has no designation'}

	designation = frappe.get_doc('Designation', employee_doc.designation)

	# Get attendance records
	attendance_records = frappe.get_all(
		'Attendance',
		filters={
			'employee': employee,
			'attendance_date': ['between', [start_date, end_date]],
			'docstatus': 1
		},
		fields=['name', 'attendance_date', 'status', 'in_time', 'out_time', 'late_entry', 'early_exit']
	)

	# Count violations
	violations = {
		'absent': {'count': 0, 'rate': designation.absent_deduction or 0, 'dates': []},
		'late': {'count': 0, 'rate': designation.late_deduction or 0, 'dates': []},
		'early_exit': {'count': 0, 'rate': designation.early_exit_deduction or 0, 'dates': []},
		'no_checkout': {'count': 0, 'rate': designation.no_checkout_deduction or 0, 'dates': []}
	}

	for att in attendance_records:
		if att.status == 'Absent':
			violations['absent']['count'] += 1
			violations['absent']['dates'].append(att.attendance_date)

		if att.late_entry == 1:
			violations['late']['count'] += 1
			violations['late']['dates'].append(att.attendance_date)

		if att.early_exit == 1:
			violations['early_exit']['count'] += 1
			violations['early_exit']['dates'].append(att.attendance_date)

		if att.status in ['Present', 'Half Day'] and not att.out_time:
			violations['no_checkout']['count'] += 1
			violations['no_checkout']['dates'].append(att.attendance_date)

	# Calculate amounts
	for key in violations:
		violations[key]['amount'] = violations[key]['count'] * violations[key]['rate']

	total_deductions = sum([v['amount'] for v in violations.values()])

	return {
		'employee': employee,
		'employee_name': employee_doc.employee_name,
		'designation': employee_doc.designation,
		'period': f"{start_date} to {end_date}",
		'violations': violations,
		'total_deductions': total_deductions
	}
