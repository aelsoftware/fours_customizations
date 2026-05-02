# Fours Customizations

ERPNext/HRMS custom app for designation-based attendance deductions and overtime management.

## Features

### 1. **Attendance Deductions**
Automatically deduct amounts from employee salaries based on attendance violations:
- **Absent Deduction** - Per absence occurrence
- **Late Deduction** - Per late arrival
- **Early Exit Deduction** - Per early departure
- **No Checkout Deduction** - When employee forgets to checkout

### 2. **Overtime Management**
Calculate and pay overtime based on designation-specific rates:
- **Flexible overtime windows** - Set start and end times per designation
- **Automatic capping** - Work beyond overtime end time is not paid
- **Hourly rate configuration** - Different rates for different job roles
- **Independent of shift** - Works separately from ERPNext's Overtime Type system

## Installation

### Prerequisites
- Frappe/ERPNext v15.x
- HRMS app installed

### Install via Bench

```bash
cd /path/to/your/bench
bench get-app https://github.com/Elvis888361/fours_customizations.git
bench --site YOUR_SITE install-app fours_customizations
```

### What Gets Installed Automatically

1. **Custom Fields on Designation DocType:**
   - Absent Deduction (Currency)
   - Late Deduction (Currency)
   - Early Exit Deduction (Currency)
   - No Checkout Deduction (Currency)
   - Overtime Start Time (Time)
   - Overtime End Time (Time)
   - Overtime Hourly Rate (Currency)

2. **Salary Components:**
   - Absent Deduction (Deduction type)
   - Late Deduction (Deduction type)
   - Early Exit Deduction (Deduction type)
   - No Checkout Deduction (Deduction type)
   - Designation Overtime Pay (Earning type)

## Configuration

### 1. Configure Designations

Go to **HR > Designation** and set values for each job role:

**Example: Manager Designation**
```
Attendance Deductions:
  - Absent Deduction: 10,000 UGX
  - Late Deduction: 5,000 UGX
  - Early Exit Deduction: 5,000 UGX
  - No Checkout Deduction: 5,000 UGX

Overtime Configuration:
  - Overtime Start Time: 17:00:00 (5:00 PM)
  - Overtime End Time: 22:00:00 (10:00 PM)
  - Overtime Hourly Rate: 8,000 UGX
```

### 2. Add Components to Salary Structure

Go to **HR > Salary Structure** and add the salary components:

**Earnings:**
- Basic Salary
- Designation Overtime Pay (if using overtime)

**Deductions:**
- Absent Deduction
- Late Deduction
- Early Exit Deduction
- No Checkout Deduction

**Note:** Set initial amounts to 0 - they will be calculated dynamically.

## Usage

### Calculating Overtime

```python
from fours_customizations.overtime_utils import calculate_designation_overtime

# Calculate overtime for an employee
overtime_data = calculate_designation_overtime(
    employee='HR-EMP-00001',
    start_date='2025-11-01',
    end_date='2025-11-30'
)

print(f"Total Hours: {overtime_data['total_hours']}")
print(f"Total Amount: {overtime_data['total_amount']}")

# View daily breakdown
for day in overtime_data['daily_breakdown']:
    print(f"{day['date']}: {day['overtime_hours']} hrs = {day['overtime_amount']}")
```

### Adding Overtime to Salary Slip

```python
from fours_customizations.overtime_utils import add_designation_overtime_to_salary_slip

# Add overtime to an existing salary slip
salary_slip = frappe.get_doc('Salary Slip', 'SAL-SLIP-001')
overtime_amount = add_designation_overtime_to_salary_slip(salary_slip)
salary_slip.save()
```

### Manual Integration (Server Script or Custom App)

Create a Server Script for `Salary Slip` on `before_save`:

```python
if doc.docstatus == 0:  # Draft
    from fours_customizations.overtime_utils import add_designation_overtime_to_salary_slip
    add_designation_overtime_to_salary_slip(doc)
```

## How It Works

### Overtime Calculation Logic

1. **Fetch attendance records** for the employee within the salary period
2. **Get designation configuration** for overtime window and rate
3. **For each attendance:**
   - If checkout time > overtime_start_time: calculate hours
   - If checkout time > overtime_end_time: cap at overtime_end_time
   - Calculate: hours × hourly_rate
4. **Sum all overtime** and return total

### Example Scenario

**Designation:** Manager
**Overtime Window:** 5:00 PM - 10:00 PM
**Hourly Rate:** 8,000 UGX

**Attendance Records:**
- Nov 4: Checkout at 8:00 PM → 3 hours × 8,000 = 24,000 UGX
- Nov 5: Checkout at 7:00 PM → 2 hours × 8,000 = 16,000 UGX
- Nov 6: Checkout at 11:00 PM → 5 hours × 8,000 = 40,000 UGX (capped at 10 PM)
- Nov 7: Checkout at 5:30 PM → 0.5 hours × 8,000 = 4,000 UGX
- Nov 8: Checkout at 4:30 PM → 0 hours (before overtime start)

**Total:** 10.5 hours = 84,000 UGX

## API Reference

### `calculate_designation_overtime(employee, start_date, end_date)`

Calculate overtime for an employee within a date range.

**Parameters:**
- `employee` (str): Employee ID
- `start_date` (str/date): Start date of period
- `end_date` (str/date): End date of period

**Returns:**
```python
{
    'total_hours': 10.5,
    'total_amount': 84000.0,
    'daily_breakdown': [
        {
            'date': '2025-11-04',
            'attendance': 'HR-ATT-2025-00001',
            'checkout_time': '2025-11-04 20:00:00',
            'overtime_hours': 3.0,
            'overtime_amount': 24000.0,
            'capped': False
        },
        # ... more days
    ],
    'designation': 'Manager',
    'overtime_start_time': '17:00:00',
    'overtime_end_time': '22:00:00',
    'hourly_rate': 8000.0
}
```

### `add_designation_overtime_to_salary_slip(salary_slip)`

Add overtime earnings to a salary slip document.

**Parameters:**
- `salary_slip`: Salary Slip doctype object

**Returns:**
- `float`: Total overtime amount added

## Deployment Checklist

- [x] Custom fields created automatically via `after_install` hook
- [x] Salary components created automatically via `after_install` hook
- [ ] Configure designations with deduction amounts and overtime rates
- [ ] Add salary components to salary structures
- [ ] Create Server Script or custom code to auto-calculate overtime on salary slips

## Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/fours_customizations
pre-commit install
```

Pre-commit is configured to use:
- ruff
- eslint
- prettier
- pyupgrade

## License

MIT

## Support

For issues and questions, please create an issue on the GitHub repository.
