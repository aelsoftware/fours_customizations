"""FOURS-SWEEP 2026-10-29: the verified majors (and the trivial minors) of the 28 Oct sweep; no live site.

    python3 -m unittest fours_customizations.tests.test_sweep_20261029

Every handler is loaded from its file under a private module name with a stub `frappe` (an in-memory table store, a
permission policy and, for raw SQL, an SQLite connection), the way test_vox_catalogue_stock does it. One TestCase per
finding id; each failed at a75b333 and passes with its fix. Bench-only behaviour (hooks firing, ERPNext validation,
real permissions) is left to the lab rehearsal.
"""

from datetime import date, datetime, time, timedelta
import importlib.util
from pathlib import Path
import re
import sqlite3
import sys
from types import ModuleType, SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

APP = Path(__file__).resolve().parents[1]


class _dict(dict):
	def __getattr__(self, key):
		if key.startswith("__"):
			raise AttributeError(key)
		return self.get(key)

	def __setattr__(self, key, value):
		self[key] = value


class ValidationError(Exception):
	pass


class PermissionError(ValidationError):  # noqa: A001 (frappe's name)
	pass


def flt(value, precision=None):
	try:
		value = float(value or 0)
	except (TypeError, ValueError):
		value = 0.0
	return round(value, precision) if precision is not None else value


def cint(value):
	try:
		return int(float(value or 0))
	except (TypeError, ValueError):
		return 0


def getdate(value=None):
	if value is None:
		return date(2026, 10, 28)
	if isinstance(value, datetime):
		return value.date()
	if isinstance(value, date):
		return value
	return date.fromisoformat(str(value)[:10])


def get_datetime(value):
	if isinstance(value, datetime):
		return value
	return datetime.fromisoformat(str(value))


def get_time(value):
	if isinstance(value, time):
		return value
	return time.fromisoformat(str(value))


def add_days(value, days):
	return getdate(value) + timedelta(days=days)


def _matches(row, filters):
	for key, cond in (filters or {}).items():
		value = row.get(key)
		if isinstance(cond, (list, tuple)):
			op, arg = cond[0], cond[1]
			if op == "in" and value not in arg:
				return False
			if op == "not in" and value in arg:
				return False
			if op == "!=" and value == arg:
				return False
			if op == "<" and not (value is not None and value < arg):
				return False
			if op == "<=" and not (value is not None and value <= arg):
				return False
			if op == ">=" and not (value is not None and value >= arg):
				return False
			if op == "is" and (arg == "set") != bool(value):
				return False
		elif value != cond:
			return False
	return True


class FakeDoc:
	"""Just enough of frappe.model.document.Document for these handlers."""

	def __init__(self, site, doctype, values=None):
		self.__dict__.update({"doctype": doctype, "flags": _dict(), "_site": site, "docstatus": 0})
		for key, value in (values or {}).items():
			if isinstance(value, list):
				value = [row if isinstance(row, FakeDoc) else FakeDoc(site, f"{doctype} Item", row) for row in value]
			self.__dict__[key] = value

	def __getattr__(self, key):
		if key.startswith("__"):
			raise AttributeError(key)
		return None

	def get(self, key, default=None):
		value = self.__dict__.get(key)
		return default if value is None else value

	def set(self, key, value):
		self.__dict__[key] = value

	def update(self, values):
		self.__dict__.update(values)

	def append(self, table, row):
		child = FakeDoc(self._site, f"{self.doctype} Item", row)
		self.__dict__.setdefault(table, []).append(child)
		return child

	@property
	def meta(self):
		return self._site.get_meta(self.doctype)

	def as_row(self):
		return _dict({k: v for k, v in self.__dict__.items() if not k.startswith("_") and k != "flags"})

	def check_permission(self, ptype="read"):
		self._site.has_permission(self.doctype, ptype, doc=self, throw=True)

	def _store(self):
		self._site.store(self)

	def insert(self, ignore_permissions=None, **_kw):
		if not (ignore_permissions or self.flags.ignore_permissions or self._site.flags.ignore_permissions):
			self.check_permission("create")
		self.__dict__.setdefault("name", self._site.next_name(self.doctype))
		self._site.log.append(("insert", self.doctype, self.name, bool(ignore_permissions or self.flags.ignore_permissions)))
		self._store()
		self._site.run_hook(self, "after_insert")
		return self

	def save(self, ignore_permissions=None, **_kw):
		self._site.log.append(("save", self.doctype, self.name))
		self._store()
		return self

	def submit(self):
		self.docstatus = 1
		self._site.log.append(("submit", self.doctype, self.name))
		self._store()
		return self

	def cancel(self):
		self.docstatus = 2
		self._site.log.append(("cancel", self.doctype, self.name))
		self._store()
		self._site.run_hook(self, "on_cancel")
		return self

	def reload(self):
		return self

	def db_set(self, field, value=None, **_kw):
		values = field if isinstance(field, dict) else {field: value}
		self.__dict__.update(values)
		self._site.log.append(("db_set", self.doctype, self.name, values))
		self._store()


class FakeSite:
	"""A stub frappe module over in-memory tables."""

	def __init__(self):
		self.tables = {}
		self.singles = {}
		self.fields = {}
		self.flags = _dict()
		self.log, self.messages, self.errors = [], [], []
		self.permit = lambda doctype, ptype, doc, user: True
		self.hooks = {}
		self.user = "user@test.invalid"
		self.sqlite = None
		self.counter = 0

	# -- store -------------------------------------------------------------------------------------------------------
	def rows(self, doctype):
		return self.tables.setdefault(doctype, [])

	def add(self, doctype, **values):
		row = _dict(values)
		row.setdefault("name", self.next_name(doctype))
		self.rows(doctype).append(row)
		return row

	def next_name(self, doctype):
		self.counter += 1
		return f"{doctype.upper().replace(' ', '-')}-NEW-{self.counter}"

	def store(self, doc):
		row = doc.as_row()
		children = {k: v for k, v in row.items() if isinstance(v, list)}
		row = _dict({k: v for k, v in row.items() if not isinstance(v, list)})
		table = self.rows(doc.doctype)
		for i, existing in enumerate(table):
			if existing.name == row.name:
				table[i] = _dict(existing, **row)
				break
		else:
			table.append(row)
		for child_rows in children.values():
			for child in child_rows:
				if isinstance(child, FakeDoc):
					child.__dict__.setdefault("name", self.next_name(child.doctype))
					child.__dict__["parent"] = row.name
					child.__dict__["docstatus"] = row.get("docstatus", 0)

	def find(self, doctype, name):
		for row in self.rows(doctype):
			if row.name == name:
				return row
		return None

	def get_meta(self, doctype):
		fields = self.fields.get(doctype)
		return SimpleNamespace(has_field=lambda f: True if fields is None else f in fields)

	def run_hook(self, doc, event):
		for hook in self.hooks.get((doc.doctype, event), ()):
			hook(doc)

	# -- frappe API --------------------------------------------------------------------------------------------------
	def get_all(self, doctype, filters=None, fields=None, pluck=None, distinct=False, order_by=None, limit=None, **_kw):
		found = [row for row in self.rows(doctype) if _matches(row, filters)]
		if pluck:
			values = [row.get(pluck) for row in found]
			return list(dict.fromkeys(values)) if distinct else values
		fields = fields or ["name"]
		out = [_dict({f.split(" as ")[-1]: row.get(f.split(" as ")[0]) for f in fields}) for row in found]
		return out[:limit] if limit else out

	def get_value(self, doctype, filters=None, fieldname="name", as_dict=False, for_update=False, **_kw):
		if filters is None:
			filters = doctype
		if isinstance(filters, str):
			row = self.find(doctype, filters)
		else:
			found = [row for row in self.rows(doctype) if _matches(row, filters)]
			row = found[0] if found else None
		if row is None:
			return None
		if isinstance(fieldname, (list, tuple)):
			values = _dict({f: row.get(f) for f in fieldname})
			return values if as_dict else tuple(values.values())
		return row.get(fieldname)

	def set_value(self, doctype, name, field, value=None, update_modified=True):
		values = field if isinstance(field, dict) else {field: value}
		row = self.find(doctype, name)
		if row is None:
			raise ValidationError(f"{doctype} {name} not found")
		row.update(values)
		self.log.append(("set_value", doctype, name, values))

	def exists(self, doctype, filters=None):
		if isinstance(filters, str) or filters is None:
			return filters if self.find(doctype, filters) else None
		found = [row for row in self.rows(doctype) if _matches(row, filters)]
		return found[0].name if found else None

	def get_single_value(self, doctype, field):
		return self.singles.get((doctype, field))

	def get_doc(self, doctype, name=None, for_update=False, **_kw):
		if isinstance(doctype, dict):
			return FakeDoc(self, doctype["doctype"], doctype)
		row = self.find(doctype, name)
		if row is None:
			raise ValidationError(f"{doctype} {name} not found")
		self.log.append(("get_doc", doctype, name, bool(for_update)))
		values = dict(row)
		for key, value in list(values.items()):
			if isinstance(value, list):
				values[key] = [FakeDoc(self, f"{doctype} Item", dict(child)) for child in value]
		return FakeDoc(self, doctype, values)

	def new_doc(self, doctype):
		return FakeDoc(self, doctype)

	def has_permission(self, doctype=None, ptype="read", doc=None, user=None, throw=False, **_kw):
		allowed = bool(self.permit(doctype, ptype, doc, user or self.user))
		if not allowed and throw:
			raise PermissionError(f"No {ptype} permission on {doctype}")
		return allowed

	def sql(self, query, values=None, as_dict=False, **_kw):
		if self.sqlite is None:
			raise AssertionError(f"Unexpected SQL: {query}")
		query = re.sub(r"%\((\w+)\)s", r":\1", query)
		params = {k: (str(v) if isinstance(v, date) else v) for k, v in (values or {}).items()}
		if isinstance(values, (list, tuple)):
			query, params = query.replace("%s", "?"), [str(v) if isinstance(v, date) else v for v in values]
		cur = self.sqlite.execute(query, params)
		rows = cur.fetchall()
		if as_dict:
			names = [d[0] for d in cur.description]
			return [_dict(zip(names, row)) for row in rows]
		return rows

	def throw(self, msg, exc=None, title=None, **_kw):
		raise (exc or ValidationError)(msg)

	def module(self):
		frappe = ModuleType("frappe")
		frappe._dict = _dict
		frappe._ = lambda text, *a: text
		frappe.ValidationError, frappe.PermissionError = ValidationError, PermissionError
		frappe.flags = self.flags
		frappe.session = _dict(user=self.user)
		frappe.local = SimpleNamespace(site="lab.test.invalid")
		frappe.conf = _dict()
		frappe.whitelist = lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda fn: fn))
		frappe.throw = self.throw
		frappe.msgprint = lambda msg, *a, **k: self.messages.append(msg)
		frappe.log_error = lambda *a, **k: self.errors.append(a)
		frappe.get_traceback = lambda *a, **k: "traceback"
		frappe.get_all = frappe.get_list = self.get_all
		frappe.get_doc, frappe.new_doc = self.get_doc, self.new_doc
		frappe.get_meta = self.get_meta
		frappe.get_cached_value = lambda doctype, name, field: self.get_value(doctype, name, field)
		frappe.has_permission = self.has_permission
		frappe.scrub = lambda text: text.lower().replace(" ", "_")
		frappe.db = SimpleNamespace(
			get_all=self.get_all, get_value=self.get_value, set_value=self.set_value, exists=self.exists,
			get_single_value=self.get_single_value, sql=self.sql, commit=lambda: None,
		)
		utils = ModuleType("frappe.utils")
		utils.flt, utils.cint, utils.getdate, utils.get_datetime, utils.get_time = flt, cint, getdate, get_datetime, get_time
		utils.add_days = add_days
		utils.nowdate = utils.today = lambda: "2026-10-28"
		utils.now_datetime = lambda: datetime(2026, 10, 28, 12, 0)
		utils.escape_html = lambda text: text
		utils.get_fullname = lambda user: user
		utils.fmt_money = lambda amount, currency=None, **k: f"{currency or ''} {flt(amount):,.2f}"
		frappe.utils = utils
		return {"frappe": frappe, "frappe.utils": utils}


def stub(name, **attrs):
	module = ModuleType(name)
	module.__dict__.update(attrs)
	return module


SETTINGS_MODULE = (
	"fours_customizations.fours_customizations.doctype.four_s_industries_settings.four_s_industries_settings"
)


class SweepCase(TestCase):
	"""Load `relpath` against a fresh FakeSite; `modules` are extra stub modules by import name."""

	def setUp(self):
		self.site = FakeSite()

	def load(self, relpath, modules=None, name=None):
		mods = self.site.module()
		package = stub("fours_customizations")
		package.__path__ = []
		mods["fours_customizations"] = package
		for dotted, module in (modules or {}).items():
			mods[dotted] = module
			if dotted.startswith("fours_customizations.") and dotted.count(".") == 1:
				setattr(package, dotted.split(".", 1)[1], module)
		self.enterContext(patch.dict(sys.modules, mods))
		spec = importlib.util.spec_from_file_location(
			name or f"fours_customizations._sweep_{Path(relpath).stem}", APP / relpath
		)
		module = importlib.util.module_from_spec(spec)
		sys.modules[spec.name] = module
		spec.loader.exec_module(module)
		return module

	def sqlite(self, *ddl):
		self.site.sqlite = sqlite3.connect(":memory:")
		for statement in ddl:
			self.site.sqlite.execute(statement)
		return self.site.sqlite

	def cancelled(self, doctype):
		return [entry[2] for entry in self.site.log if entry[:2] == ("cancel", doctype)]


class TestSweep165LandedCostUom(SweepCase):
	"""#165: a Landed Cost Voucher's new selling price is a stock-UOM price; carton rows scale by their factor."""

	def setUp(self):
		super().setUp()
		price_lists = stub(
			"fours_customizations.price_lists", split_active=lambda: False, automatic_price_refused=lambda *a: False
		)
		self.mod = self.load("landed_cost_handler.py", {"fours_customizations.price_lists": price_lists})
		self.site.add("Item", name="X", stock_uom="Nos")
		self.site.add("UOM Conversion Detail", parenttype="Item", parent="X", uom="Box", conversion_factor=12)
		self.site.singles[("Selling Settings", "selling_price_list")] = "Standard Selling"

	def price(self, name, uom, rate):
		return self.site.add(
			"Item Price", name=name, item_code="X", price_list="Standard Selling", selling=1, uom=uom, price_list_rate=rate
		)

	def test_carton_row_scaled_unit_rows_set(self):
		unit, blank, box = self.price("IP-NOS", "Nos", 1000), self.price("IP-BLANK", None, 1000), self.price("IP-BOX", "Box", 12000)
		self.mod.on_submit(SimpleNamespace(name="LCV-1", company="4S", items=[SimpleNamespace(item_code="X", new_selling_price=1100)]))
		self.assertEqual(unit.price_list_rate, 1100)
		self.assertEqual(blank.price_list_rate, 1100)
		self.assertEqual(box.price_list_rate, 13200)  # 12 x 1100, not the unit price

	def test_row_in_unknown_uom_left_alone(self):
		odd = self.price("IP-CASE", "Case", 50000)
		unit = self.price("IP-NOS", "Nos", 1000)
		self.assertEqual(self.mod._sync_item_price("X", 1100, "Standard Selling"), 1)
		self.assertEqual(odd.price_list_rate, 50000)
		self.assertEqual(unit.price_list_rate, 1100)

	def test_created_price_is_in_stock_uom(self):
		self.assertEqual(self.mod._sync_item_price("X", 1100, "Standard Selling"), 1)
		(created,) = self.site.rows("Item Price")
		self.assertEqual((created.uom, created.price_list_rate), ("Nos", 1100))


class TestSweep173AutoCancelAuthz(SweepCase):
	"""#173: the draft-delivery fast track cancels only invoices the requester may read (company User Permissions)."""

	def setUp(self):
		super().setUp()
		utils = stub(
			"cancellation_requests.utils",
			build_document_link=lambda *a: "", create_notification_log=lambda *a, **k: None,
			get_doctype_config=lambda doctype: {"enabled": 1}, get_settings=lambda: None,
			post_to_slack_webhook=lambda *a: None, resolve_cancellation_recipients=lambda *a, **k: [],
			send_slack_dm=lambda *a: None,
		)
		chain = stub(
			"fours_customizations.sales_chain_integrity",
			VOX_NANSANA_PAID_CANCEL_FLAG="flag", can_auto_cancel_nansana_embedded_payment=lambda *a, **k: False,
		)
		self.mod = self.load(
			"cancellation_override.py",
			{"cancellation_requests": stub("cancellation_requests"), "cancellation_requests.utils": utils,
			 "fours_customizations.sales_chain_integrity": chain},
		)
		self.site.add("Sales Invoice", name="SI-4S-1", company="4S Industries Limited", docstatus=1, is_return=0, items=[])
		self.site.add("Delivery Note Item", parent="DN-1", against_sales_invoice="SI-4S-1")
		self.site.add("Delivery Note", name="DN-1", is_return=0, docstatus=0)
		self.perform = self.enterContext(patch.object(self.mod, "_perform_auto_cancellation"))
		self.enterContext(patch.object(self.mod, "_notify"))

	def test_requester_without_read_on_invoice_is_deferred(self):
		self.site.permit = lambda doctype, ptype, doc, user: not (doctype == "Sales Invoice" and doc is not None)
		self.assertIsNone(self.mod._try_auto_cancel_invoice("SI-4S-1", "wrong customer"))
		self.perform.assert_not_called()

	def test_requester_with_read_is_fast_tracked(self):
		seen = []
		self.site.permit = lambda doctype, ptype, doc, user: seen.append((doctype, ptype, getattr(doc, "name", None))) or True
		self.assertIn("automatically cancelled", self.mod._try_auto_cancel_invoice("SI-4S-1", "wrong customer")["message"])
		self.perform.assert_called_once()
		self.assertIn(("Sales Invoice", "read", "SI-4S-1"), seen)


class TestSweep174CheckinPermission(SweepCase):
	"""#174: add_checkin is the caller's own Employee Checkin insert, permission-checked (no ignore_permissions)."""

	def setUp(self):
		super().setUp()
		settings = stub(SETTINGS_MODULE, get_settings=lambda: _dict())
		self.mod = self.load("checkin_handler.py", {SETTINGS_MODULE: settings})
		self.site.add("Employee", name="HR-EMP-4S", company="4S Industries Limited")
		self.site.add("Employee", name="HR-EMP-VOX", company="Vox Lounge Nansana")

	def punch(self, employee="HR-EMP-4S", at="2026-10-28 09:00:00"):
		return self.mod.add_checkin(employee_field_value=employee, timestamp=at, log_type="IN")

	def test_user_without_checkin_create_is_refused(self):
		self.site.permit = lambda doctype, ptype, doc, user: doctype != "Employee Checkin"
		with self.assertRaises(PermissionError):
			self.punch()
		self.assertEqual(self.site.rows("Employee Checkin"), [])

	def test_other_company_employee_is_refused(self):
		# User Permission on Company: the document-level check refuses the other company's employee
		self.site.permit = lambda doctype, ptype, doc, user: doc is None or doc.employee == "HR-EMP-4S"
		with self.assertRaises(PermissionError):
			self.punch("HR-EMP-VOX")
		self.assertEqual(self.site.rows("Employee Checkin"), [])
		self.assertEqual(self.punch()["status"], "created")

	def test_duplicate_of_other_company_punch_is_refused(self):
		self.site.add("Employee Checkin", name="CHK-1", employee="HR-EMP-VOX", time=datetime(2026, 10, 28, 9))
		self.site.permit = lambda doctype, ptype, doc, user: doc is None or doc.employee == "HR-EMP-4S"
		with self.assertRaises(PermissionError):
			self.punch("HR-EMP-VOX")

	def test_permitted_insert_is_not_elevated(self):
		self.assertEqual(self.punch()["status"], "created")
		inserts = [entry for entry in self.site.log if entry[:2] == ("insert", "Employee Checkin")]
		self.assertEqual(len(inserts), 1)
		self.assertFalse(inserts[0][3], "the device punch insert must not run with ignore_permissions")
