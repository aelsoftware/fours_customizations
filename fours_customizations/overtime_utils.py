"""Utility functions for calculating designation-based overtime"""

import frappe
from frappe import _
from frappe.utils import (
	getdate,
	get_datetime,
	time_diff_in_hours,
	get_time,
	now_datetime
)
from datetime import datetime, time as dt_time, timedelta


def calculate_designation_overtime(employee, start_date, end_date):
	"""
	Calculate overtime hours and payment for an employee based on their designation's overtime configuration.

	Args:
		employee (str): Employee ID
		start_date (str/date): Start date of the period
		end_date (str/date): End date of the period

	Returns:
		dict: {
			'total_hours': float,
			'total_amount': float,
			'daily_breakdown': list of dicts with daily overtime details
		}
	"""

	# Get employee and designation
	emp_doc = frappe.get_doc('Employee', employee)

	if not emp_doc.designation:
		return {
			'total_hours': 0,
			'total_amount': 0,
			'daily_breakdown': [],
			'error': 'Employee has no designation assigned'
		}

	designation = frappe.get_doc('Designation', emp_doc.designation)

	# Check if designation has overtime configuration
	if not designation.overtime_start_time or not designation.overtime_end_time or not designation.overtime_hourly_rate:
		return {
			'total_hours': 0,
			'total_amount': 0,
			'daily_breakdown': [],
			'note': f'Designation {designation.name} has no overtime configuration'
		}

	# Get all attendance records for the period with checkout times
	attendance_records = frappe.get_all(
		'Attendance',
		filters={
			'employee': employee,
			'attendance_date': ['between', [start_date, end_date]],
			'status': ['in', ['Present', 'Half Day']],
			'docstatus': 1  # Only submitted attendance
		},
		fields=['name', 'attendance_date', 'in_time', 'out_time', 'status'],
		order_by='attendance_date'
	)

	total_hours = 0.0
	total_amount = 0.0
	daily_breakdown = []

	for attendance in attendance_records:
		if not attendance.out_time:
			# No checkout time, skip this record
			continue

		overtime_info = calculate_daily_overtime(
			attendance.out_time,
			designation.overtime_start_time,
			designation.overtime_end_time,
			designation.overtime_hourly_rate,
			attendance.attendance_date
		)

		if overtime_info['hours'] > 0:
			total_hours += overtime_info['hours']
			total_amount += overtime_info['amount']

			daily_breakdown.append({
				'date': attendance.attendance_date,
				'attendance': attendance.name,
				'checkout_time': attendance.out_time,
				'overtime_hours': overtime_info['hours'],
				'overtime_amount': overtime_info['amount'],
				'capped': overtime_info['capped']
			})

	return {
		'total_hours': round(total_hours, 2),
		'total_amount': round(total_amount, 2),
		'daily_breakdown': daily_breakdown,
		'designation': designation.name,
		'overtime_start_time': designation.overtime_start_time,
		'overtime_end_time': designation.overtime_end_time,
		'hourly_rate': designation.overtime_hourly_rate
	}


def calculate_daily_overtime(checkout_datetime, overtime_start_time, overtime_end_time, hourly_rate, attendance_date):
	"""
	Calculate overtime for a single day.

	Args:
		checkout_datetime (datetime): The actual checkout datetime
		overtime_start_time (time): Overtime window start time
		overtime_end_time (time): Overtime window end time (cap)
		hourly_rate (float): Hourly overtime rate
		attendance_date (date): Date of attendance

	Returns:
		dict: {'hours': float, 'amount': float, 'capped': bool}
	"""

	if not checkout_datetime:
		return {'hours': 0, 'amount': 0, 'capped': False}

	# Convert to datetime objects
	checkout_dt = get_datetime(checkout_datetime)

	# Get the date portion
	date_obj = getdate(attendance_date)

	# Create datetime objects for overtime start and end on the same date
	overtime_start_dt = datetime.combine(date_obj, get_time(overtime_start_time))
	overtime_end_dt = datetime.combine(date_obj, get_time(overtime_end_time))

	# Handle cases where overtime end is past midnight (next day)
	if get_time(overtime_end_time) < get_time(overtime_start_time):
		overtime_end_dt += timedelta(days=1)

	# If checkout is before overtime start, no overtime
	if checkout_dt <= overtime_start_dt:
		return {'hours': 0, 'amount': 0, 'capped': False}

	# Determine the effective end time (checkout or overtime cap, whichever is earlier)
	capped = False
	if checkout_dt > overtime_end_dt:
		effective_end_dt = overtime_end_dt
		capped = True
	else:
		effective_end_dt = checkout_dt

	# Calculate hours between overtime start and effective end
	hours = time_diff_in_hours(effective_end_dt, overtime_start_dt)

	# Ensure non-negative hours
	if hours < 0:
		hours = 0

	# Calculate amount
	amount = hours * hourly_rate

	return {
		'hours': round(hours, 2),
		'amount': round(amount, 2),
		'capped': capped
	}


def add_designation_overtime_to_salary_slip(salary_slip):
	"""
	Calculate and add designation-based overtime to a salary slip.

	Args:
		salary_slip: Salary Slip doctype object

	Returns:
		float: Total overtime amount added
	"""

	if not salary_slip.employee:
		return 0

	# Calculate overtime for the salary period
	overtime_data = calculate_designation_overtime(
		salary_slip.employee,
		salary_slip.start_date,
		salary_slip.end_date
	)

	if overtime_data['total_amount'] <= 0:
		return 0

	# Check if overtime component already exists in earnings
	component_name = 'Designation Overtime Pay'
	existing_component = None

	for earning in salary_slip.earnings:
		if earning.salary_component == component_name:
			existing_component = earning
			break

	if existing_component:
		# Update existing component
		existing_component.amount = overtime_data['total_amount']
	else:
		# Add new component
		salary_slip.append('earnings', {
			'salary_component': component_name,
			'amount': overtime_data['total_amount']
		})

	return overtime_data['total_amount']
