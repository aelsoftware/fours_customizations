import frappe


def create_test_data():
	"""Create test data for attendance deduction system"""

	frappe.set_user("Administrator")

	# Step 1: Create/Update Test Designation with deduction amounts
	print("\n" + "="*60)
	print("STEP 1: Creating/Updating Designation")
	print("="*60)

	if not frappe.db.exists('Designation', 'Test Manager'):
		designation = frappe.get_doc({
			'doctype': 'Designation',
			'designation_name': 'Test Manager',
			'description': 'Test designation for attendance deduction testing'
		})
		designation.insert(ignore_permissions=True)
		print("✓ Created new designation: Test Manager")
	else:
		designation = frappe.get_doc('Designation', 'Test Manager')
		print("✓ Using existing designation: Test Manager")

	# Update with deduction amounts
	designation.absent_deduction = 10000
	designation.late_deduction = 5000
	designation.early_exit_deduction = 5000
	designation.no_checkout_deduction = 5000

	# Add overtime configuration
	designation.overtime_start_time = '17:00:00'  # 5:00 PM
	designation.overtime_end_time = '22:00:00'    # 10:00 PM
	designation.overtime_hourly_rate = 8000       # 8,000 UGX per hour

	designation.save(ignore_permissions=True)

	print(f"  - Absent Deduction: {designation.absent_deduction:,.0f} UGX")
	print(f"  - Late Deduction: {designation.late_deduction:,.0f} UGX")
	print(f"  - Early Exit Deduction: {designation.early_exit_deduction:,.0f} UGX")
	print(f"  - No Checkout Deduction: {designation.no_checkout_deduction:,.0f} UGX")

	print(f"\n  Overtime Configuration:")
	print(f"  - Overtime Start Time: {designation.overtime_start_time}")
	print(f"  - Overtime End Time: {designation.overtime_end_time}")
	print(f"  - Overtime Hourly Rate: {designation.overtime_hourly_rate:,.0f} UGX")

	# Step 2: Create Salary Components for deductions
	print("\n" + "="*60)
	print("STEP 2: Creating Salary Components")
	print("="*60)

	components = [
		{
			'name': 'Absent Deduction',
			'salary_component': 'Absent Deduction',
			'description': 'Deduction for absence',
			'type': 'Deduction'
		},
		{
			'name': 'Late Deduction',
			'salary_component': 'Late Deduction',
			'description': 'Deduction for late arrival',
			'type': 'Deduction'
		},
		{
			'name': 'Early Exit Deduction',
			'salary_component': 'Early Exit Deduction',
			'description': 'Deduction for early exit',
			'type': 'Deduction'
		},
		{
			'name': 'No Checkout Deduction',
			'salary_component': 'No Checkout Deduction',
			'description': 'Deduction for not checking out',
			'type': 'Deduction'
		},
		{
			'name': 'Basic Salary',
			'salary_component': 'Basic Salary',
			'description': 'Basic salary component',
			'type': 'Earning'
		}
	]

	for comp_data in components:
		if not frappe.db.exists('Salary Component', comp_data['name']):
			comp = frappe.get_doc({
				'doctype': 'Salary Component',
				'salary_component': comp_data['salary_component'],
				'description': comp_data['description'],
				'type': comp_data['type']
			})
			comp.insert(ignore_permissions=True)
			print(f"✓ Created: {comp_data['name']} ({comp_data['type']})")
		else:
			print(f"✓ Already exists: {comp_data['name']}")

	# Step 3: Create Salary Structure
	print("\n" + "="*60)
	print("STEP 3: Creating Salary Structure")
	print("="*60)

	salary_structure_name = 'Test Salary Structure with Deductions'

	if frappe.db.exists('Salary Structure', salary_structure_name):
		frappe.delete_doc('Salary Structure', salary_structure_name, force=True)
		print("✓ Deleted existing salary structure")

	salary_structure = frappe.get_doc({
		'doctype': 'Salary Structure',
		'name': salary_structure_name,
		'company': frappe.defaults.get_defaults().get('company') or 'Test Company',
		'payroll_frequency': 'Monthly',
		'is_active': 'Yes',
		'earnings': [
			{
				'salary_component': 'Basic Salary',
				'amount_based_on_formula': 0,
				'amount': 1000000
			}
		],
		'deductions': [
			{
				'salary_component': 'Absent Deduction',
				'amount_based_on_formula': 0,
				'amount': 0
			},
			{
				'salary_component': 'Late Deduction',
				'amount_based_on_formula': 0,
				'amount': 0
			},
			{
				'salary_component': 'Early Exit Deduction',
				'amount_based_on_formula': 0,
				'amount': 0
			},
			{
				'salary_component': 'No Checkout Deduction',
				'amount_based_on_formula': 0,
				'amount': 0
			}
		]
	})
	salary_structure.insert(ignore_permissions=True)
	salary_structure.submit()
	print(f"✓ Created and submitted: {salary_structure_name}")
	print(f"  - Earnings: 1 component (Basic Salary: 1,000,000 UGX)")
	print(f"  - Deductions: 4 components (attendance-based)")

	# Step 4: Create Test Employee
	print("\n" + "="*60)
	print("STEP 4: Creating Test Employee")
	print("="*60)

	employee_id = 'EMP-TEST-001'

	if frappe.db.exists('Employee', employee_id):
		frappe.delete_doc('Employee', employee_id, force=True)
		print("✓ Deleted existing test employee")

	# Get company
	company = frappe.defaults.get_defaults().get('company') or 'Test Company'

	employee = frappe.get_doc({
		'doctype': 'Employee',
		'employee': employee_id,
		'first_name': 'John',
		'last_name': 'Doe',
		'gender': 'Male',
		'date_of_birth': '1990-01-01',
		'date_of_joining': '2024-01-01',
		'company': company,
		'designation': 'Test Manager',
		'status': 'Active',
		'employee_number': 'EMP-001'
	})
	employee.insert(ignore_permissions=True)
	print(f"✓ Created employee: {employee.employee_name} ({employee.name})")
	print(f"  - Designation: {employee.designation}")
	print(f"  - Company: {employee.company}")

	# Assign salary structure to employee
	salary_structure_assignment = frappe.get_doc({
		'doctype': 'Salary Structure Assignment',
		'employee': employee.name,
		'salary_structure': salary_structure_name,
		'from_date': '2024-01-01',
		'company': company,
		'base': 1000000
	})
	salary_structure_assignment.insert(ignore_permissions=True)
	salary_structure_assignment.submit()
	print(f"✓ Assigned salary structure to employee")
	print(f"  - Base Salary: 1,000,000 UGX")

	frappe.db.commit()

	print("\n" + "="*60)
	print("✓ TEST DATA CREATION COMPLETED!")
	print("="*60)
	print("\nSummary:")
	print(f"  - Designation: Test Manager (with 4 deduction amounts)")
	print(f"  - Salary Components: 5 created (1 earning + 4 deductions)")
	print(f"  - Salary Structure: {salary_structure_name}")
	print(f"  - Employee: {employee.employee_name} ({employee.name})")
	print(f"\nNext: Create a Salary Slip for {employee.employee_name}")
	print("="*60 + "\n")

	return {
		'designation': designation.name,
		'employee': employee.name,
		'salary_structure': salary_structure_name
	}


def update_designation_overtime_config():
	"""Update Test Manager designation with overtime configuration"""
	frappe.set_user("Administrator")

	print("\n" + "="*60)
	print("UPDATING DESIGNATION OVERTIME CONFIGURATION")
	print("="*60)

	designation = frappe.get_doc('Designation', 'Test Manager')
	designation.overtime_start_time = '17:00:00'  # 5:00 PM
	designation.overtime_end_time = '22:00:00'    # 10:00 PM
	designation.overtime_hourly_rate = 8000       # 8,000 UGX per hour
	designation.save(ignore_permissions=True)
	frappe.db.commit()

	print(f"✓ Updated: {designation.name}")
	print(f"  - Overtime Start: {designation.overtime_start_time}")
	print(f"  - Overtime End: {designation.overtime_end_time}")
	print(f"  - Hourly Rate: {designation.overtime_hourly_rate:,.0f} UGX")
	print("="*60 + "\n")


def create_test_attendance_with_overtime():
	"""Create test attendance records with overtime hours"""
	frappe.set_user("Administrator")

	from frappe.utils import today, add_days, get_datetime
	from datetime import datetime, time

	print("\n" + "="*60)
	print("CREATING TEST ATTENDANCE WITH OVERTIME")
	print("="*60)

	# Get test employee
	employee_list = frappe.get_all('Employee',
		filters={'designation': 'Test Manager', 'status': 'Active'},
		fields=['name'],
		limit=1
	)

	if not employee_list:
		print("❌ Error: No test employee found")
		return

	employee_id = employee_list[0].name
	employee = frappe.get_doc('Employee', employee_id)

	print(f"Creating attendance for: {employee.employee_name} ({employee.name})")

	# Define test scenarios
	today_date = today()

	test_scenarios = [
		{
			'date': add_days(today_date, -5),
			'in_time': '08:00:00',
			'out_time': '20:00:00',  # 8 PM (3 hours overtime: 5 PM - 8 PM)
			'description': '3 hours overtime (5 PM - 8 PM)'
		},
		{
			'date': add_days(today_date, -4),
			'in_time': '08:00:00',
			'out_time': '19:00:00',  # 7 PM (2 hours overtime: 5 PM - 7 PM)
			'description': '2 hours overtime (5 PM - 7 PM)'
		},
		{
			'date': add_days(today_date, -3),
			'in_time': '08:00:00',
			'out_time': '23:00:00',  # 11 PM (5 hours overtime capped: 5 PM - 10 PM)
			'description': '5 hours overtime CAPPED (worked till 11 PM, paid till 10 PM)'
		},
		{
			'date': add_days(today_date, -2),
			'in_time': '08:00:00',
			'out_time': '17:30:00',  # 5:30 PM (0.5 hours overtime: 5 PM - 5:30 PM)
			'description': '0.5 hours overtime (5 PM - 5:30 PM)'
		},
		{
			'date': add_days(today_date, -1),
			'in_time': '08:00:00',
			'out_time': '16:30:00',  # 4:30 PM (No overtime - before 5 PM)
			'description': 'No overtime (left before 5 PM)'
		},
	]

	created_count = 0
	for scenario in test_scenarios:
		att_date = scenario['date']

		# Check if attendance already exists
		if frappe.db.exists('Attendance', {'employee': employee_id, 'attendance_date': att_date}):
			print(f"  - Skipping {att_date}: Already exists")
			continue

		# Create datetime objects
		in_datetime = get_datetime(f"{att_date} {scenario['in_time']}")
		out_datetime = get_datetime(f"{att_date} {scenario['out_time']}")

		# Create attendance record
		attendance = frappe.get_doc({
			'doctype': 'Attendance',
			'employee': employee_id,
			'employee_name': employee.employee_name,
			'attendance_date': att_date,
			'status': 'Present',
			'company': employee.company,
			'in_time': in_datetime,
			'out_time': out_datetime
		})

		attendance.insert(ignore_permissions=True)
		attendance.submit()

		created_count += 1
		print(f"  ✓ {att_date}: {scenario['description']}")

	frappe.db.commit()

	print(f"\n✓ Created {created_count} attendance record(s)")
	print("="*60 + "\n")

	return created_count


def test_overtime_calculation():
	"""Test the overtime calculation with the created attendance records"""
	frappe.set_user("Administrator")

	from fours_customizations.overtime_utils import calculate_designation_overtime
	from frappe.utils import today, add_days, get_first_day, get_last_day

	print("\n" + "="*60)
	print("TESTING OVERTIME CALCULATION")
	print("="*60)

	# Get test employee
	employee_list = frappe.get_all('Employee',
		filters={'designation': 'Test Manager', 'status': 'Active'},
		fields=['name', 'employee_name'],
		limit=1
	)

	if not employee_list:
		print("❌ Error: No test employee found")
		return

	employee_id = employee_list[0].name
	employee_name = employee_list[0].employee_name

	print(f"Employee: {employee_name} ({employee_id})")

	# Calculate overtime for current month
	today_date = today()
	start_date = get_first_day(today_date)
	end_date = get_last_day(today_date)

	print(f"Period: {start_date} to {end_date}\n")

	# Calculate overtime
	overtime_data = calculate_designation_overtime(employee_id, start_date, end_date)

	# Display results
	print("-" * 60)
	print("OVERTIME SUMMARY")
	print("-" * 60)
	print(f"Designation: {overtime_data.get('designation', 'N/A')}")
	print(f"Overtime Window: {overtime_data.get('overtime_start_time', 'N/A')} - {overtime_data.get('overtime_end_time', 'N/A')}")
	print(f"Hourly Rate: {overtime_data.get('hourly_rate', 0):,.0f} UGX")
	print(f"\nTotal Overtime Hours: {overtime_data['total_hours']:.2f}")
	print(f"Total Overtime Amount: {overtime_data['total_amount']:,.2f} UGX")

	if overtime_data.get('daily_breakdown'):
		print(f"\n{'-' * 60}")
		print("DAILY BREAKDOWN")
		print(f"{'-' * 60}")
		print(f"{'Date':<12} {'Checkout':<10} {'Hours':<8} {'Amount':<12} {'Status'}")
		print(f"{'-' * 60}")

		for day in overtime_data['daily_breakdown']:
			checkout_time = str(day['checkout_time']).split(' ')[1][:5] if ' ' in str(day['checkout_time']) else str(day['checkout_time'])[:5]
			capped_status = "CAPPED" if day['capped'] else ""

			print(f"{str(day['date']):<12} {checkout_time:<10} {day['overtime_hours']:<8.2f} {day['overtime_amount']:<12,.0f} {capped_status}")

		print(f"{'-' * 60}")

	elif overtime_data.get('note'):
		print(f"\nNote: {overtime_data['note']}")
	elif overtime_data.get('error'):
		print(f"\n❌ Error: {overtime_data['error']}")

	print("="*60 + "\n")

	return overtime_data


def create_overtime_salary_component():
	"""Create salary component for designation-based overtime"""
	frappe.set_user("Administrator")

	print("\n" + "="*60)
	print("CREATING OVERTIME SALARY COMPONENT")
	print("="*60)

	component_name = 'Designation Overtime Pay'

	if not frappe.db.exists('Salary Component', component_name):
		comp = frappe.get_doc({
			'doctype': 'Salary Component',
			'salary_component': component_name,
			'description': 'Overtime payment based on designation overtime rate',
			'type': 'Earning'
		})
		comp.insert(ignore_permissions=True)
		print(f"✓ Created: {component_name} (Earning)")
	else:
		print(f"✓ Already exists: {component_name}")

	frappe.db.commit()
	print("="*60 + "\n")


def verify_designation_fields():
	"""Verify all custom fields on Designation doctype"""
	frappe.set_user("Administrator")

	custom_fields = frappe.get_all(
		'Custom Field',
		filters={'dt': 'Designation'},
		fields=['fieldname', 'label', 'fieldtype'],
		order_by='idx'
	)

	print('\n' + '='*70)
	print('CUSTOM FIELDS ON DESIGNATION DOCTYPE')
	print('='*70)
	for field in custom_fields:
		label = field.label or "(No Label)"
		print(f'{label:40} | {field.fieldtype:15} | {field.fieldname}')
	print('='*70)
	print(f'\nTotal: {len(custom_fields)} custom fields\n')


def check_salary_structure():
	"""Check the salary structure configuration"""
	frappe.set_user("Administrator")

	ss = frappe.get_doc('Salary Structure', 'Test Salary Structure with Deductions')
	print('\n' + '='*60)
	print('SALARY STRUCTURE CHECK')
	print('='*60)
	print(f'Name: {ss.name}')
	print(f'Status: {ss.docstatus} (1=submitted, 0=draft)')

	print('\nEarnings:')
	for e in ss.earnings:
		print(f'  - {e.salary_component}: {e.amount:,.0f}')

	print('\nDeductions:')
	for d in ss.deductions:
		print(f'  - {d.salary_component}: {d.amount:,.0f}')

	print('='*60 + '\n')


def delete_test_salary_slips():
	"""Delete all test salary slips for the test employee"""
	frappe.set_user("Administrator")

	# Find all salary slips for test employee
	salary_slips = frappe.get_all('Salary Slip',
		filters={'employee': 'HR-EMP-00002'},
		fields=['name']
	)

	for slip in salary_slips:
		frappe.delete_doc('Salary Slip', slip.name, force=True)
		print(f"✓ Deleted: {slip.name}")

	frappe.db.commit()
	print(f"\nDeleted {len(salary_slips)} salary slip(s)")


def add_overtime_to_existing_slip():
	"""Add overtime to the existing salary slip"""
	frappe.set_user("Administrator")

	from fours_customizations.overtime_utils import calculate_designation_overtime
	from frappe.utils import get_first_day, get_last_day, today

	print("\n" + "="*60)
	print("ADDING OVERTIME TO EXISTING SALARY SLIP")
	print("="*60)

	# Get existing slip
	slips = frappe.get_all('Salary Slip',
		filters={'employee': 'HR-EMP-00002'},
		fields=['name'],
		order_by='creation desc',
		limit=1
	)

	if not slips:
		print("❌ No salary slip found")
		return

	slip = frappe.get_doc('Salary Slip', slips[0].name)

	print(f"Salary Slip: {slip.name}")
	print(f"Employee: {slip.employee_name}")

	# Calculate overtime
	overtime_data = calculate_designation_overtime(slip.employee, slip.start_date, slip.end_date)

	if overtime_data['total_amount'] > 0:
		# Check if overtime already exists
		has_overtime = False
		for earning in slip.earnings:
			if earning.salary_component == 'Designation Overtime Pay':
				earning.amount = overtime_data['total_amount']
				has_overtime = True
				break

		if not has_overtime:
			slip.append('earnings', {
				'salary_component': 'Designation Overtime Pay',
				'amount': overtime_data['total_amount']
			})

		# Recalculate totals
		slip.gross_pay = sum([e.amount for e in slip.earnings])
		slip.net_pay = slip.gross_pay - slip.total_deduction

		slip.save(ignore_permissions=True)
		frappe.db.commit()

		print(f"\n✓ Added Overtime:")
		print(f"  - Total Hours: {overtime_data['total_hours']:.2f}")
		print(f"  - Total Amount: {overtime_data['total_amount']:,.0f} UGX")
		print(f"\n✓ Updated Salary Slip:")
		print(f"  - New Gross Pay: {slip.gross_pay:,.0f} UGX")
		print(f"  - Net Pay: {slip.net_pay:,.0f} UGX")

	else:
		print("\nNo overtime to add")

	print("="*60 + "\n")


def view_existing_salary_slip():
	"""View the existing salary slip"""
	frappe.set_user("Administrator")

	slips = frappe.get_all('Salary Slip',
		filters={'employee': 'HR-EMP-00002'},
		fields=['name', 'start_date', 'end_date', 'gross_pay', 'total_deduction', 'net_pay'],
		order_by='creation desc',
		limit=1
	)

	if not slips:
		print("No salary slip found")
		return

	slip_name = slips[0].name
	slip = frappe.get_doc('Salary Slip', slip_name)

	print(f'\n{'='*60}')
	print(f'EXISTING SALARY SLIP: {slip.name}')
	print(f'{'='*60}')
	print(f'Employee: {slip.employee_name} ({slip.employee})')
	print(f'Period: {slip.start_date} to {slip.end_date}\n')

	print('EARNINGS:')
	for e in slip.earnings:
		print(f'  + {e.salary_component}: {e.amount:,.0f} UGX')
	print(f'  {'='*50}')
	print(f'  Gross Pay: {slip.gross_pay:,.0f} UGX\n')

	print('DEDUCTIONS:')
	for d in slip.deductions:
		if d.amount > 0:
			print(f'  - {d.salary_component}: {d.amount:,.0f} UGX')
	print(f'  {'='*50}')
	print(f'  Total Deductions: {slip.total_deduction:,.0f} UGX\n')

	print(f'{'*'*60}')
	print(f'NET PAY: {slip.net_pay:,.0f} UGX')
	print(f'{'*'*60}\n')


def create_comprehensive_salary_slip(employee_id=None):
	"""Create a comprehensive salary slip with both deductions and overtime"""
	frappe.set_user("Administrator")

	from fours_customizations.overtime_utils import calculate_designation_overtime
	from frappe.utils import today, get_first_day, get_last_day, getdate

	print("\n" + "="*60)
	print("CREATING COMPREHENSIVE SALARY SLIP")
	print("="*60)

	# Get the test employee
	if not employee_id:
		employee_list = frappe.get_all('Employee',
			filters={'designation': 'Test Manager', 'status': 'Active'},
			fields=['name'],
			limit=1
		)
		if not employee_list:
			print("❌ Error: No employee found with designation 'Test Manager'")
			return None
		employee_id = employee_list[0].name

	employee = frappe.get_doc('Employee', employee_id)
	designation = frappe.get_doc('Designation', employee.designation)

	# Create/Set Holiday List if not exists
	current_year = getdate(today()).year
	holiday_list_name = f'Test Holiday List {current_year}'

	if not frappe.db.exists('Holiday List', holiday_list_name):
		holiday_list = frappe.get_doc({
			'doctype': 'Holiday List',
			'holiday_list_name': holiday_list_name,
			'from_date': f'{current_year}-01-01',
			'to_date': f'{current_year}-12-31'
		})
		holiday_list.insert(ignore_permissions=True)

	# Set holiday list on company if not set
	company_doc = frappe.get_doc('Company', employee.company)
	if not company_doc.default_holiday_list:
		company_doc.default_holiday_list = holiday_list_name
		company_doc.save(ignore_permissions=True)

	# Get current month
	posting_date = today()
	start_date = get_first_day(posting_date)
	end_date = get_last_day(posting_date)

	# Create salary slip
	salary_slip = frappe.get_doc({
		'doctype': 'Salary Slip',
		'employee': employee.name,
		'employee_name': employee.employee_name,
		'posting_date': posting_date,
		'start_date': start_date,
		'end_date': end_date,
		'company': employee.company
	})

	# Get salary structure assignment
	salary_slip.salary_slip_based_on_timesheet = 0
	salary_slip.get_emp_and_working_day_details()

	print(f"\nEmployee: {employee.employee_name} ({employee.name})")
	print(f"Period: {start_date} to {end_date}")
	print(f"Designation: {employee.designation}")

	# Add deduction components manually (simulating 2 absences, 1 late, 1 early exit)
	print("\n" + "-"*60)
	print("ADDING ATTENDANCE DEDUCTIONS")
	print("-"*60)

	deductions_to_add = [
		{'salary_component': 'Absent Deduction', 'amount': 2 * designation.absent_deduction},
		{'salary_component': 'Late Deduction', 'amount': 1 * designation.late_deduction},
		{'salary_component': 'Early Exit Deduction', 'amount': 1 * designation.early_exit_deduction},
		{'salary_component': 'No Checkout Deduction', 'amount': 1 * designation.no_checkout_deduction}
	]

	for ded_data in deductions_to_add:
		salary_slip.append('deductions', {
			'salary_component': ded_data['salary_component'],
			'amount': ded_data['amount']
		})
		print(f"  ✓ {ded_data['salary_component']}: {ded_data['amount']:,.0f} UGX")

	# Calculate and add overtime
	print("\n" + "-"*60)
	print("CALCULATING OVERTIME")
	print("-"*60)

	overtime_data = calculate_designation_overtime(employee.name, start_date, end_date)

	if overtime_data['total_amount'] > 0:
		salary_slip.append('earnings', {
			'salary_component': 'Designation Overtime Pay',
			'amount': overtime_data['total_amount']
		})
		print(f"  ✓ Total Overtime Hours: {overtime_data['total_hours']:.2f}")
		print(f"  ✓ Overtime Amount: {overtime_data['total_amount']:,.0f} UGX")

		if overtime_data.get('daily_breakdown'):
			print(f"\n  Daily Breakdown:")
			for day in overtime_data['daily_breakdown']:
				status = " (CAPPED)" if day['capped'] else ""
				print(f"    - {day['date']}: {day['overtime_hours']:.2f} hrs = {day['overtime_amount']:,.0f} UGX{status}")
	else:
		print("  No overtime for this period")

	# Manually calculate totals
	salary_slip.gross_pay = sum([d.amount for d in salary_slip.earnings])
	salary_slip.total_deduction = sum([d.amount for d in salary_slip.deductions])
	salary_slip.net_pay = salary_slip.gross_pay - salary_slip.total_deduction

	salary_slip.insert(ignore_permissions=True)

	print("\n" + "="*60)
	print("SALARY SLIP SUMMARY")
	print("="*60)
	print(f"Salary Slip: {salary_slip.name}")

	print("\nEARNINGS:")
	for earning in salary_slip.earnings:
		print(f"  + {earning.salary_component}: {earning.amount:,.0f} UGX")
	print(f"  {'='*50}")
	print(f"  Gross Pay: {salary_slip.gross_pay:,.0f} UGX")

	print("\nDEDUCTIONS:")
	for deduction in salary_slip.deductions:
		if deduction.amount > 0:
			print(f"  - {deduction.salary_component}: {deduction.amount:,.0f} UGX")
	print(f"  {'='*50}")
	print(f"  Total Deductions: {salary_slip.total_deduction:,.0f} UGX")

	print(f"\n{'*'*60}")
	print(f"NET PAY: {salary_slip.net_pay:,.0f} UGX")
	print(f"{'*'*60}")

	frappe.db.commit()

	print("\n" + "="*60)
	print("✓ COMPREHENSIVE SALARY SLIP CREATED!")
	print("="*60 + "\n")

	return salary_slip.name


def create_test_salary_slip(employee_id=None):
	"""Create a test salary slip with attendance deductions"""

	frappe.set_user("Administrator")

	print("\n" + "="*60)
	print("CREATING TEST SALARY SLIP")
	print("="*60)

	# Get the test employee - find by designation if not provided
	if not employee_id:
		employee_list = frappe.get_all('Employee',
			filters={'designation': 'Test Manager', 'status': 'Active'},
			fields=['name'],
			limit=1
		)
		if not employee_list:
			print("❌ Error: No employee found with designation 'Test Manager'")
			return None
		employee_id = employee_list[0].name

	employee = frappe.get_doc('Employee', employee_id)
	designation = frappe.get_doc('Designation', employee.designation)

	# Create/Set Holiday List if not exists
	from frappe.utils import today, get_first_day, get_last_day, getdate

	current_year = getdate(today()).year
	holiday_list_name = f'Test Holiday List {current_year}'

	if not frappe.db.exists('Holiday List', holiday_list_name):
		holiday_list = frappe.get_doc({
			'doctype': 'Holiday List',
			'holiday_list_name': holiday_list_name,
			'from_date': f'{current_year}-01-01',
			'to_date': f'{current_year}-12-31'
		})
		holiday_list.insert(ignore_permissions=True)
		print(f"✓ Created Holiday List: {holiday_list_name}")

	# Set holiday list on company if not set
	company_doc = frappe.get_doc('Company', employee.company)
	if not company_doc.default_holiday_list:
		company_doc.default_holiday_list = holiday_list_name
		company_doc.save(ignore_permissions=True)
		print(f"✓ Set default holiday list for company: {employee.company}")

	# Get current month
	from frappe.utils import today, get_first_day, get_last_day

	posting_date = today()
	start_date = get_first_day(posting_date)
	end_date = get_last_day(posting_date)

	# Create salary slip
	salary_slip = frappe.get_doc({
		'doctype': 'Salary Slip',
		'employee': employee.name,
		'employee_name': employee.employee_name,
		'posting_date': posting_date,
		'start_date': start_date,
		'end_date': end_date,
		'company': employee.company
	})

	# Get salary structure assignment
	salary_slip.salary_slip_based_on_timesheet = 0
	salary_slip.get_emp_and_working_day_details()

	# Add manual deductions based on attendance (simulating 2 absences, 1 late, 1 early exit)
	print("\nSimulating attendance violations:")
	print(f"  - Absences: 2 times × {designation.absent_deduction:,.0f} = {2 * designation.absent_deduction:,.0f} UGX")
	print(f"  - Late arrivals: 1 time × {designation.late_deduction:,.0f} = {1 * designation.late_deduction:,.0f} UGX")
	print(f"  - Early exits: 1 time × {designation.early_exit_deduction:,.0f} = {1 * designation.early_exit_deduction:,.0f} UGX")
	print(f"  - No checkout: 1 time × {designation.no_checkout_deduction:,.0f} = {1 * designation.no_checkout_deduction:,.0f} UGX")

	# Manually add deduction rows (since get_emp_and_working_day_details skips 0 amounts)
	print(f"\nAdding deduction components manually...")

	deductions_to_add = [
		{'salary_component': 'Absent Deduction', 'amount': 2 * designation.absent_deduction},
		{'salary_component': 'Late Deduction', 'amount': 1 * designation.late_deduction},
		{'salary_component': 'Early Exit Deduction', 'amount': 1 * designation.early_exit_deduction},
		{'salary_component': 'No Checkout Deduction', 'amount': 1 * designation.no_checkout_deduction}
	]

	for ded_data in deductions_to_add:
		salary_slip.append('deductions', {
			'salary_component': ded_data['salary_component'],
			'amount': ded_data['amount']
		})
		print(f"  ✓ Added: {ded_data['salary_component']} = {ded_data['amount']:,.0f} UGX")

	# Manually calculate totals to avoid resetting our custom amounts
	salary_slip.gross_pay = sum([d.amount for d in salary_slip.earnings])
	salary_slip.total_deduction = sum([d.amount for d in salary_slip.deductions])
	salary_slip.net_pay = salary_slip.gross_pay - salary_slip.total_deduction

	salary_slip.insert(ignore_permissions=True)

	print(f"\n✓ Salary Slip Created: {salary_slip.name}")
	print("\n" + "-"*60)
	print("SALARY SLIP DETAILS")
	print("-"*60)
	print(f"Employee: {salary_slip.employee_name} ({salary_slip.employee})")
	print(f"Period: {salary_slip.start_date} to {salary_slip.end_date}")
	print(f"Designation: {employee.designation}")

	print("\nEARNINGS:")
	for earning in salary_slip.earnings:
		print(f"  + {earning.salary_component}: {earning.amount:,.0f} UGX")
	print(f"  {'='*50}")
	print(f"  Gross Pay: {salary_slip.gross_pay:,.0f} UGX")

	print("\nDEDUCTIONS:")
	for deduction in salary_slip.deductions:
		if deduction.amount > 0:
			print(f"  - {deduction.salary_component}: {deduction.amount:,.0f} UGX")
	print(f"  {'='*50}")
	print(f"  Total Deductions: {salary_slip.total_deduction:,.0f} UGX")

	print(f"\n{'*'*60}")
	print(f"NET PAY: {salary_slip.net_pay:,.0f} UGX")
	print(f"{'*'*60}")

	frappe.db.commit()

	print("\n" + "="*60)
	print("✓ SALARY SLIP CREATION COMPLETED!")
	print("="*60)
	print(f"\nYou can view the salary slip in ERPNext:")
	print(f"  HR > Salary Slip > {salary_slip.name}")
	print("="*60 + "\n")

	return salary_slip.name
