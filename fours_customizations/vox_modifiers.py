"""Opt-in Vox menu modifiers with private, append-only POS evidence.

This provider uses the existing Kit modifier protocol. It never changes an Item,
BOM, selling price, stock balance or ledger. The approved side/topping prices
are explicitly sales-only: their unknown portions are not guessed. Publications
and selections freeze this policy, so a future recipe cannot reinterpret them.

The caller owns every transaction. Catalogue and invoice verification are reads;
only explicit Administrator publication and authenticated connector resolution
can insert audit records. No helper commits or queues work.
"""

from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import frappe

OPTION = "Vox Kitchen Modifier Option"
SELECTION = "Vox Kitchen Modifier Selection"
CONFIG = "vox_kitchen_modifiers"
APPROVED_COMPANY = "Vox Lounge Nansana"
INVOICE_FIELD = "custom_kit_pos_modifier_reference"
PAYLOAD_FIELD = "_kit_pos_modifiers"
PROVIDER = "fours_customizations.vox_modifiers.provider"
MENU = {"VXN-DRY-GOAT": 15000, "VXN-PAN-FRIED-GOAT": 20000, "VXN-CHICKEN": 15000}
SIDES = ("Chips", "Rice", "Matooke", "Posho", "Wedges", "Mashed Potatoes")
VERSION = 1
MAX_OPTIONS = 64
_INSERT = ContextVar("vox_private_modifier_insert", default=None)
_RECORD_FIELDS = (
	"company",
	"kit_pos_sale",
	"item_code",
	"line_id",
	"public_id",
	"previous_selection",
	"snapshot",
	"snapshot_hash",
	"recorded_by",
)
_BINDING_FIELDS = (
	"company",
	"sale",
	"sale_uuid",
	"line_id",
	"desktop_line_uuid",
	"item_code",
	"product_id",
	"variation_id",
	"sub_unit_id",
	"quantity",
	"uom",
	"stock_uom",
	"conversion_factor",
)
_OPTION_FIELDS = {
	"version",
	"company",
	"item_code",
	"group_name",
	"label",
	"price",
	"currency",
	"stock_policy",
}


class ModifierError(ValueError):
	pass


def _invalid():
	raise ModifierError("The selected modifiers do not match the company, item or recorded bill.")


def _text(raw, *, empty=False, limit=140):
	if raw is None and empty:
		return ""
	if (
		not isinstance(raw, str)
		or (not raw and not empty)
		or raw != raw.strip()
		or len(raw) > limit
		or any(ord(character) < 32 for character in raw)
	):
		_invalid()
	return raw


def _number(raw, *, zero=False):
	if isinstance(raw, bool) or raw in (None, ""):
		_invalid()
	try:
		result = Decimal(str(raw))
	except (InvalidOperation, ValueError, TypeError):
		_invalid()
	if not result.is_finite() or result < 0 or (not zero and not result) or result > Decimal("1e16"):
		_invalid()
	return result


def _identifier(raw, *, zero=False):
	if zero and raw in (None, ""):
		return 0
	value = _number(raw, zero=zero)
	if value != value.to_integral_value() or value >= 2**53:
		_invalid()
	return int(value)


def _native_name(raw):
	"""Canonical positive Kit ID, whether native int or a text Link/parent.

	Do not coerce arbitrary values: bools, floats, padded/decimal/exponent text
	and leading-zero aliases are not document identities. Frozen snapshots keep
	the same canonical text representation, independent of the DB return type.
	"""
	if type(raw) not in (int, str):
		_invalid()
	if isinstance(raw, str):
		_text(raw)
	value = str(_identifier(raw))
	if isinstance(raw, str) and raw != value:
		_invalid()
	return value


def _json_value(raw):
	if isinstance(raw, Decimal):
		return format(raw, "f").rstrip("0").rstrip(".") if "." in format(raw, "f") else format(raw, "f")
	if isinstance(raw, dict):
		return {key: _json_value(value) for key, value in raw.items()}
	if isinstance(raw, list | tuple):
		return [_json_value(value) for value in raw]
	return raw


def canonical(raw):
	return json.dumps(_json_value(raw), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(raw):
	if isinstance(raw, str):
		raw = json.loads(raw)
	return hashlib.sha256(canonical(raw).encode()).hexdigest()


def _public_id(snapshot):
	# Exact in JavaScript; a unique database key also fences a hash collision.
	return int(fingerprint(snapshot)[:13], 16) or 1


def _enabled(company):
	# An unrelated company must not be affected by this narrowly opted-in menu.
	if company != APPROVED_COMPANY:
		return False
	configured = (getattr(frappe, "conf", None) or {}).get(CONFIG, {})
	if not isinstance(configured, dict):
		raise ValueError("Vox modifier configuration must be a company mapping.")
	raw = configured.get(company, False)
	if type(raw) not in (bool, int) or raw not in (0, 1):
		raise ValueError("Vox modifier enablement must be an explicit boolean or 0/1.")
	return bool(raw)


def _schema_ready():
	return all(frappe.db.exists("DocType", doctype) for doctype in (OPTION, SELECTION))


def _parent(company, code, *, allow_disabled=True):
	if company != APPROVED_COMPANY or code not in MENU or not frappe.db.exists("Item", code):
		_invalid()
	item = frappe.get_doc("Item", code)
	if not item.get("is_sales_item") or item.get("has_variants") or item.get("stock_uom") != "Pc(s)":
		_invalid()
	defaults = item.get("item_defaults") or []
	if defaults and not any(row.get("company") == company for row in defaults):
		_invalid()
	if not allow_disabled and item.get("disabled"):
		return None
	return item


def _location_signature(location):
	return {
		field: location.get(field) or ""
		for field in (
			"name",
			"company",
			"warehouse",
			"selling_price_list",
			"pos_profile",
		)
	}


def _sale_location(sale):
	from kit_pos.connector.materialize import _location_for

	# A header may have a new location_id before its denormalized Link is saved.
	return _location_for(frappe._dict(location_id=sale.get("location_id")))


def _check_location(company, location, sale=None):
	if (
		frappe.session.user == "Guest"
		or company != APPROVED_COMPANY
		or not isinstance(location, dict)
		or location.get("company") != company
	):
		_invalid()
	if not location.get("name") or not location.get("warehouse"):
		_invalid()
	warehouse = frappe.db.get_value(
		"Warehouse",
		location["warehouse"],
		["company", "is_group"],
		as_dict=True,
	)
	if not warehouse or warehouse.get("company") != company or warehouse.get("is_group"):
		_invalid()
	if sale is not None:
		resolved = _sale_location(sale)
		if not resolved or _location_signature(resolved) != _location_signature(location):
			_invalid()


def _option_snapshot(company, item_code, group, label, price):
	return {
		"version": VERSION,
		"company": company,
		"item_code": item_code,
		"group_name": group,
		"label": label,
		"price": _number(price, zero=True),
		"currency": "UGX",
		"stock_policy": "sales_only",
	}


def _approved_options(company, item_code):
	for group, price, prefix in (("Sides", 10000, ""), ("Extra Toppings", 5000, "Extra ")):
		for label in SIDES:
			yield _option_snapshot(company, item_code, group, prefix + label, price)


def _record_values(doctype, snapshot):
	digest = fingerprint(snapshot)
	return {
		"doctype": doctype,
		"name": ("VKMO-" if doctype == OPTION else "VKMS-") + digest,
		"company": snapshot["company"],
		"item_code": snapshot["item_code"],
		"kit_pos_sale": snapshot.get("sale") or "",
		"line_id": snapshot.get("line_id") or "",
		"public_id": str(_public_id(snapshot)) if doctype == OPTION else "",
		"previous_selection": snapshot.get("previous_selection") or "",
		"snapshot": canonical(snapshot),
		"snapshot_hash": digest,
		"recorded_by": frappe.session.user,
	}


@dataclass(frozen=True)
class _InsertContext:
	doctype: str
	name: str
	signature: str
	actor: str
	site: str


def _signature(doc):
	values = {field: doc.get(field) or "" for field in _RECORD_FIELDS}
	if doc.doctype == SELECTION:
		# Native Link validation replaces "123" with the linked name 123 before
		# before_insert. That representation change is not an authority change.
		values["kit_pos_sale"] = _native_name(doc.get("kit_pos_sale"))
		values["line_id"] = _native_name(doc.get("line_id"))
	return fingerprint(
		{
			"doctype": doc.doctype,
			"name": doc.name,
			**values,
		}
	)


def _read_snapshot(doc):
	try:
		raw = json.loads(doc.snapshot)
		if (
			not isinstance(raw, dict)
			or type(raw.get("version")) is not int
			or raw["version"] != VERSION
			or raw.get("company") != APPROVED_COMPANY
			or raw["company"] != doc.company
			or raw.get("item_code") not in MENU
			or raw["item_code"] != doc.item_code
			or fingerprint(raw) != doc.snapshot_hash
		):
			_invalid()
		if doc.doctype == OPTION:
			if (
				set(raw) != _OPTION_FIELDS
				or doc.name != "VKMO-" + doc.snapshot_hash
				or str(_public_id(raw)) != doc.public_id
				or raw["currency"] != "UGX"
				or raw["stock_policy"] != "sales_only"
			):
				_invalid()
			_number(raw["price"], zero=True)
			_text(raw["group_name"], limit=255)
			_text(raw["label"], limit=255)
		elif doc.doctype == SELECTION:
			if (
				set(raw) != {"version", *_BINDING_FIELDS, "options", "previous_selection"}
				or doc.name != "VKMS-" + doc.snapshot_hash
				or raw["sale"] != _native_name(doc.kit_pos_sale)
				or raw["line_id"] != _native_name(doc.line_id)
				or raw["previous_selection"] != (doc.previous_selection or "")
				or not isinstance(raw["options"], list)
				or len(raw["options"]) > MAX_OPTIONS
			):
				_invalid()
			_number(raw["quantity"])
			_number(raw["conversion_factor"])
			for field in ("sale", "sale_uuid", "line_id", "uom", "stock_uom"):
				_text(raw[field])
			identities = []
			for option in raw["options"]:
				if not isinstance(option, dict) or set(option) != {"option", "public_id", "snapshot_hash"}:
					_invalid()
				identities.append(_identifier(option["public_id"]))
			if identities != sorted(set(identities)):
				_invalid()
		else:
			_invalid()
		return raw
	except (TypeError, ValueError, AttributeError, KeyError, json.JSONDecodeError):
		_invalid()


def validate_private_record(doc, method=None):
	context = _INSERT.get()
	if (
		not isinstance(context, _InsertContext)
		or not doc.is_new()
		or context.doctype != doc.doctype
		or context.name != doc.name
		or context.actor != frappe.session.user
		or context.site != frappe.local.site
		or context.signature != _signature(doc)
	):
		frappe.throw(
			"Modifier audit records can only be created by the authorized POS service.",
			frappe.PermissionError,
		)
	_read_snapshot(doc)


def prevent_private_change(doc=None, *args, **kwargs):
	frappe.throw("Modifier audit history cannot be edited, deleted or renamed.", frappe.PermissionError)


def _insert(snapshot, doctype):
	values = _record_values(doctype, snapshot)
	if frappe.db.exists(doctype, values["name"]):
		doc = frappe.get_doc(doctype, values["name"])
		if canonical(_read_snapshot(doc)) != canonical(snapshot):
			_invalid()
		return doc
	doc = frappe.get_doc(values)
	token = _INSERT.set(
		_InsertContext(doc.doctype, doc.name, _signature(doc), frappe.session.user, frappe.local.site)
	)
	try:
		doc.insert(ignore_permissions=True, set_name=doc.name)
	finally:
		_INSERT.reset(token)
	return doc


def _published(snapshot):
	name = "VKMO-" + fingerprint(snapshot)
	if not frappe.db.exists(OPTION, name):
		raise ValueError("Publish the approved Vox modifiers before enabling this catalogue.")
	doc = frappe.get_doc(OPTION, name)
	if canonical(_read_snapshot(doc)) != canonical(snapshot):
		_invalid()
	return doc


def publish_approved_options(company=APPROVED_COMPANY):
	"""Explicit setup; permits disabled parents so publication can precede launch."""
	if frappe.session.user != "Administrator":
		frappe.throw("Only Administrator may publish this approved menu.", frappe.PermissionError)
	if not _enabled(company) or not _schema_ready():
		raise ValueError("Opt in the company and install modifier audit storage before publication.")
	if frappe.db.get_value("Company", company, "default_currency") != "UGX":
		raise ValueError("This reviewed menu is priced in UGX.")
	# Validate every parent before the first publication. No Item/price/BOM save.
	for code in MENU:
		_parent(company, code)
	created = existing = 0
	for code in MENU:
		for snapshot in _approved_options(company, code):
			already = bool(frappe.db.exists(OPTION, "VKMO-" + fingerprint(snapshot)))
			_insert(snapshot, OPTION)
			existing += int(already)
			created += int(not already)
	return {
		"company": company,
		"options_created": created,
		"options_existing": existing,
		"parents": len(MENU),
		"stock_policy": "sales_only",
		"stock_transactions_created": 0,
	}


def _public_option(snapshot, public_id):
	return {
		"variation_id": int(public_id),
		"name": snapshot["label"],
		"price": float(_number(snapshot["price"], zero=True)),
	}


def _catalogue(company, item_codes, *, allow_disabled=False):
	if not _enabled(company) or not item_codes:
		return {}
	if not _schema_ready():
		raise ValueError("Vox modifier audit storage is unavailable.")
	result = {}
	for code in sorted(set(item_codes) & set(MENU)):
		if not _parent(company, code, allow_disabled=allow_disabled):
			continue
		groups = {}
		for snapshot in _approved_options(company, code):
			doc = _published(snapshot)
			option = _read_snapshot(doc)
			group = groups.setdefault(
				option["group_name"],
				{
					"id": _public_id(
						{
							"version": VERSION,
							"company": company,
							"item_code": code,
							"group": option["group_name"],
						}
					),
					"name": option["group_name"],
					"modifiers": [],
				},
			)
			group["modifiers"].append(_public_option(option, doc.public_id))
		result[code] = [
			{**group, "modifiers": sorted(group["modifiers"], key=lambda value: value["variation_id"])}
			for group in groups.values()
		]
	return result


def preview_publications(company=APPROVED_COMPANY):
	"""Administrator read-only public-shape probe, including not-yet-enabled Items."""
	if frappe.session.user != "Administrator":
		frappe.throw("Only Administrator may preview an unpublished menu launch.", frappe.PermissionError)
	return _catalogue(company, list(MENU), allow_disabled=True)


def _payload(line):
	try:
		raw = line.get("line_payload") if line else None
		decoded = json.loads(raw) if isinstance(raw, str) else raw
		if decoded is not None and not isinstance(decoded, dict):
			_invalid()
		return decoded or {}
	except (TypeError, ValueError):
		_invalid()


def _reference(line):
	proof = _payload(line).get(PAYLOAD_FIELD)
	if proof is None:
		return ""
	if (
		not isinstance(proof, dict)
		or type(proof.get("version")) is not int
		or proof.get("version") != VERSION
	):
		_invalid()
	return _text(proof.get("reference"))


def _resolved_line(line):
	from kit_pos.connector import ids

	result = dict(line.as_dict() if hasattr(line, "as_dict") else line)
	product = _identifier(result.get("product_id"), zero=True)
	variation = _identifier(result.get("variation_id"), zero=True)
	codes = {ids.lookup("Item", identity) for identity in (product, variation) if identity}
	if len(codes) != 1 or None in codes:
		_invalid()
	code = next(iter(codes))
	if result.get("item_code") not in (None, "", code):
		_invalid()
	result["item_code"] = code
	return result


def _binding(company, sale, line):
	from kit_pos.connector.item_uom import selected_uom

	line = _resolved_line(line)
	name, uuid = _native_name(sale.get("name")), _text(sale.get("sale_uuid"))
	if line.get("parent") not in (None, "") and _native_name(line.get("parent")) != name:
		_invalid()
	if frappe.db.get_value("Kit POS Sale", name, "sale_uuid") != uuid:
		_invalid()
	item = _parent(company, line["item_code"])
	subunit = _identifier(line.get("sub_unit_id"), zero=True)
	payload = _payload(line)
	uom, factor = selected_uom(
		item.name,
		item.stock_uom,
		subunit,
		client_multiplier=payload.get("base_unit_multiplier"),
		client_unit_name=payload.get("unit_name"),
	)
	return {
		"company": company,
		"sale": name,
		"sale_uuid": uuid,
		"line_id": _native_name(line.get("line_id")),
		"desktop_line_uuid": _text(line.get("desktop_line_uuid"), empty=True),
		"item_code": item.name,
		"product_id": str(_identifier(line.get("product_id"), zero=True)),
		"variation_id": str(_identifier(line.get("variation_id"), zero=True)),
		"sub_unit_id": str(subunit),
		"quantity": _number(line.get("quantity")),
		"uom": uom,
		"stock_uom": item.stock_uom,
		"conversion_factor": _number(factor),
	}


def _same_binding(snapshot, binding):
	return canonical({field: snapshot.get(field) for field in _BINDING_FIELDS}) == canonical(binding)


def _load_selection(reference):
	if not reference or not frappe.db.exists(SELECTION, reference):
		_invalid()
	doc = frappe.get_doc(SELECTION, reference)
	return doc, _read_snapshot(doc)


def _selection_options(snapshot):
	result = []
	for row in snapshot["options"]:
		doc = frappe.get_doc(OPTION, row["option"])
		option = _read_snapshot(doc)
		if (
			doc.snapshot_hash != row["snapshot_hash"]
			or int(doc.public_id) != row["public_id"]
			or option["company"] != snapshot["company"]
			or option["item_code"] != snapshot["item_code"]
		):
			_invalid()
		result.append((option, int(doc.public_id)))
	return result


def _public_selection(doc, snapshot):
	return {
		"reference": doc.name if snapshot["options"] else "",
		"modifiers": [_public_option(option, identity) for option, identity in _selection_options(snapshot)],
	}


def _resolve_options(company, item_code, selection, amount):
	rows = [selection.get(field) for field in ("modifier", "modifier_price", "modifier_quantity")]
	if any(not isinstance(row, list) for row in rows) or len({len(row) for row in rows}) != 1:
		_invalid()
	identities, prices, amounts = rows
	if len(identities) > MAX_OPTIONS:
		_invalid()
	options, seen = [], set()
	for identity, price, selected_amount in zip(identities, prices, amounts, strict=True):
		if type(identity) is not int or identity in seen or _number(selected_amount) != amount:
			_invalid()
		identity = _identifier(identity)
		seen.add(identity)
		name = frappe.db.get_value(OPTION, {"public_id": str(identity)}, "name")
		if not name:
			_invalid()
		doc = frappe.get_doc(OPTION, name)
		option = _read_snapshot(doc)
		if (
			option["company"] != company
			or option["item_code"] != item_code
			or _number(option["price"], zero=True) != _number(price, zero=True)
		):
			_invalid()
		options.append({"option": doc.name, "public_id": identity, "snapshot_hash": doc.snapshot_hash})
	return sorted(options, key=lambda row: row["public_id"])


def _verify_previous(company, sale, previous_line):
	if previous_line is None:
		return None, None
	reference = _reference(previous_line)
	name = _native_name(sale.get("name"))
	parent = previous_line.get("parent")
	parent = name if parent in (None, "") else _native_name(parent)
	previous_sale = sale if parent == name else frappe.get_doc("Kit POS Sale", parent)
	if parent != name:
		# Only core merge_sell supplies another bill's persisted source line.
		# Independently fence company, profile, opening and original membership.
		if (
			previous_sale.get("deleted_at")
			or previous_sale.get("merged_into")
			or _native_name(previous_sale.get("location_id")) != _native_name(sale.get("location_id"))
			or previous_sale.get("pos_opening_entry") != sale.get("pos_opening_entry")
			or not _sale_location(previous_sale)
			or _sale_location(previous_sale).get("company") != company
		):
			_invalid()
		originals = [
			row
			for row in previous_sale.get("lines") or []
			if _native_name(row.get("line_id")) == _native_name(previous_line.get("line_id"))
		]
		if (
			len(originals) != 1
			or _reference(originals[0]) != reference
			or canonical(_binding(company, previous_sale, originals[0]))
			!= canonical(_binding(company, previous_sale, previous_line))
		):
			_invalid()
	if not reference:
		return None, None
	doc, snapshot = _load_selection(reference)
	if not _same_binding(snapshot, _binding(company, previous_sale, previous_line)):
		_invalid()
	return doc, snapshot


def _verify_merge_target(company, sale, line, previous_line, options, source_snapshot):
	"""Combining portions must not give either bill the other's different extras."""
	if previous_line is None or previous_line.get("parent") in (None, ""):
		return
	if _native_name(previous_line.get("parent")) == _native_name(sale.get("name")):
		return
	if options != (source_snapshot["options"] if source_snapshot else []):
		_invalid()
	uuid = line.get("desktop_line_uuid")
	targets = [
		candidate
		for candidate in sale.get("lines") or []
		if (
			_native_name(candidate.get("line_id")) == _native_name(line.get("line_id"))
			or (uuid and candidate.get("desktop_line_uuid") == uuid)
		)
	]
	if not targets:
		return  # A new target line adopts the verified source selection unchanged.
	if len(targets) != 1 or _native_name(targets[0].get("line_id")) != _native_name(line.get("line_id")):
		_invalid()
	target = targets[0]
	target_qty = _number(target.get("quantity"), zero=True)
	target_reference = _reference(target)
	if target_reference:
		_target_doc, target_snapshot = _load_selection(target_reference)
		if not target_qty or not _same_binding(target_snapshot, _binding(company, sale, target)):
			_invalid()
		_selection_options(target_snapshot)
		target_options = target_snapshot["options"]
	else:
		if any(
			_payload(target).get(field)
			for field in (
				"modifier",
				"modifier_price",
				"modifier_quantity",
				"modifiers",
			)
		):
			_invalid()
		target_options = []
	if not target_qty:
		return  # A genuinely empty, unselected placeholder adds no old portions.
	if target_options != options or _number(line.get("quantity")) != target_qty + _number(
		previous_line.get("quantity")
	):
		_invalid()


def _resolve(company, sale, line, previous_line, selection):
	if not _schema_ready():
		_invalid()
	if sale.get("deleted_at") or sale.get("merged_into") or sale.get("materialize_state") == "voided":
		_invalid()
	binding = _binding(company, sale, line)
	options = _resolve_options(company, binding["item_code"], selection, binding["quantity"])
	previous, old = _verify_previous(company, sale, previous_line)
	_verify_merge_target(company, sale, line, previous_line, options, old)
	if old and _same_binding(old, binding) and old["options"] == options:
		return _public_selection(previous, old)
	if not options and not previous:
		return {"reference": "", "modifiers": []}
	snapshot = {
		"version": VERSION,
		**binding,
		"options": options,
		"previous_selection": previous.name if previous else "",
	}
	# A deterministic audit key makes transaction retries idempotent even when
	# the connector has not yet persisted the returned opaque line reference.
	doc = _insert(snapshot, SELECTION)
	return _public_selection(doc, _read_snapshot(doc))


def _invoice(company, sale, line, reference):
	doc, snapshot = _load_selection(reference)
	if not snapshot["options"] or not _same_binding(snapshot, _binding(company, sale, line)):
		_invalid()
	return _public_selection(doc, snapshot)


def provider(*, operation, company, location, **kwargs):
	"""Internal Kit hook; never whitelisted and never grants extra product access."""
	if operation == "catalogue" and not _enabled(company):
		return {}
	try:
		_check_location(company, location, kwargs.get("sale"))
		if operation == "catalogue":
			return _catalogue(company, kwargs["item_codes"])
		# Keep existing published IDs verifiable after the catalogue opt-in is
		# disabled, including offline bills whose first sync happens afterwards.
		if operation == "resolve":
			return _resolve(
				company, kwargs["sale"], kwargs["line"], kwargs.get("previous_line"), kwargs["selection"]
			)
		if operation == "invoice":
			return _invoice(company, kwargs["sale"], kwargs["line"], kwargs["reference"])
		_invalid()
	except ModifierError:
		from kit_pos.connector.router import ApiError

		raise ApiError(
			422,
			{
				"error": {
					"code": "INVALID_MODIFIER_SELECTION",
					"message": "The selected modifiers do not match this item, company or recorded price. Keep the original bill and ask management to review.",
				}
			},
		) from None


def _sale_references(sale):
	refs = []
	for row in sale.get("lines") or []:
		payload = _payload(row)
		selected = any(
			payload.get(field) for field in ("modifier", "modifier_price", "modifier_quantity", "modifiers")
		)
		reference = _reference(row)
		if selected and not reference:
			_invalid()
		if reference:
			if reference in refs:
				_invalid()
			refs.append(reference)
	return refs


def invoice_options(invoice, row):
	"""Private read: verify native invoice provenance, UOM, quantity and charges."""
	reference = _text(row.get(INVOICE_FIELD), empty=True)
	if not reference:
		return []
	if sum(candidate.get(INVOICE_FIELD) == reference for candidate in invoice.get("items") or []) != 1:
		_invalid()
	doc, snapshot = _load_selection(reference)
	if snapshot["company"] != invoice.company or snapshot["item_code"] != row.item_code:
		_invalid()
	from kit_pos.connector.materialize import _invoice_submission_context, _pos_profile_for

	sale = frappe.get_doc("Kit POS Sale", snapshot["sale"])
	location = _sale_location(sale)
	_check_location(invoice.company, location, sale)
	context = _invoice_submission_context(invoice)
	context_matches = bool(
		context
		and _native_name(context.sale.get("name")) == _native_name(sale.get("name"))
		and _text(context.sale.get("sale_uuid")) == _text(sale.get("sale_uuid"))
	)
	warehouse_matches = invoice.get("set_warehouse") == location.get("warehouse")
	if not warehouse_matches:
		from fours_customizations.vox_work_orders import _invoice_warehouse_header_matches

		warehouse_matches = _invoice_warehouse_header_matches(invoice, location)
	if (
		not (sale.get("sales_invoice") == invoice.name or context_matches)
		or (context and not context_matches)
		or (context and _location_signature(context.location) != _location_signature(location))
		or invoice.get("pos_profile") != _pos_profile_for(location)
		or not warehouse_matches
	):
		_invalid()
	lines = [
		line for line in sale.get("lines") or [] if _native_name(line.get("line_id")) == snapshot["line_id"]
	]
	if len(lines) != 1 or _reference(lines[0]) != reference:
		_invalid()
	line = _resolved_line(lines[0])
	if (
		not _same_binding(snapshot, _binding(invoice.company, sale, line))
		or _number(row.qty) != _number(snapshot["quantity"])
		or row.uom != snapshot["uom"]
		or row.stock_uom != snapshot["stock_uom"]
		or _number(row.conversion_factor) != _number(snapshot["conversion_factor"])
	):
		_invalid()
	options = _selection_options(snapshot)
	base = line.get("unit_price_inc_tax")
	if base in (None, ""):
		base = line.get("unit_price") or 0
	expected_rate = _number(base, zero=True) + sum(
		(_number(option["price"], zero=True) for option, _ in options), Decimal(0)
	)
	if _number(row.rate, zero=True) != expected_rate:
		_invalid()
	return [dict(option, public_id=identity, selection=doc.name) for option, identity in options]


def validate_invoice_modifiers(invoice, method=None):
	"""Before submit/update-after-submit: stripping the opaque field is not a bypass."""
	if invoice.get("company") != APPROVED_COMPANY:
		return
	from kit_pos.connector.materialize import _invoice_submission_context

	try:
		actual = [_text(row.get(INVOICE_FIELD), empty=True) for row in invoice.get("items") or []]
		actual = [reference for reference in actual if reference]
		if len(actual) != len(set(actual)):
			_invalid()
		context = _invoice_submission_context(invoice)
		linked = (
			frappe.get_all(
				"Kit POS Sale", filters={"sales_invoice": invoice.name}, fields=["name"], limit_page_length=2
			)
			if invoice.get("name")
			else []
		)
		if len(linked) > 1:
			_invalid()
		context_name = _native_name(context.sale.get("name")) if context else None
		linked_name = _native_name(linked[0]["name"]) if linked else None
		if context and linked and linked_name != context_name:
			_invalid()
		name = context_name if context else linked_name
		if not name:
			if actual:
				_invalid()
			return
		sale = frappe.get_doc("Kit POS Sale", name)
		if _native_name(sale.get("name")) != name or (
			context and _text(context.sale.get("sale_uuid")) != _text(sale.get("sale_uuid"))
		):
			_invalid()
		if set(actual) != set(_sale_references(sale)):
			_invalid()
		for row in invoice.get("items") or []:
			if row.get(INVOICE_FIELD):
				invoice_options(invoice, row)
	except ModifierError:
		frappe.throw(
			"Invoice modifiers no longer match the original POS bill. Reload its verified selections before submitting."
		)


def private_has_permission(doc, user=None, permission_type=None):
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	return bool(
		permission_type in (None, "read", "select", "report")
		and doc.get("company") == APPROVED_COMPANY
		and "System Manager" in frappe.get_roles(user)
		and frappe.has_permission("Company", "read", doc=doc.company, user=user)
	)


def _private_query_conditions(doctype, user=None):
	user = user or frappe.session.user
	if user == "Administrator":
		return ""
	if "System Manager" not in frappe.get_roles(user) or not frappe.has_permission(
		"Company", "read", doc=APPROVED_COMPANY, user=user
	):
		return "1 = 0"
	return f"`tab{doctype}`.company = " + frappe.db.escape(APPROVED_COMPANY)


def option_query_conditions(user=None):
	return _private_query_conditions(OPTION, user)


def selection_query_conditions(user=None):
	return _private_query_conditions(SELECTION, user)
