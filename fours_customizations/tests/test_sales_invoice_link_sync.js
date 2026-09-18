"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SCRIPT_PATH = path.resolve(__dirname, "../public/js/sales_invoice.js");
const SCRIPT = fs.readFileSync(SCRIPT_PATH, "utf8");

function loadResolver(overrides = {}) {
	const frappe = {
		ui: { form: { on() {} } },
		utils: { escape_html: (value) => value },
		throw(message) {
			throw new Error(message);
		},
		...overrides,
	};
	const context = {
		frappe,
		__: (message) => message,
		flt: Number,
	};
	vm.runInNewContext(SCRIPT, context, { filename: SCRIPT_PATH });
	return context.resolve_typed_link;
}

test("waits for pending native Link validation before recovering the visible Customer", async () => {
	let releaseAjax;
	const pendingAjax = new Promise((resolve) => {
		releaseAjax = resolve;
	});
	const validationCalls = [];
	const frm = {
		doc: { customer: undefined },
		fields_dict: {},
		async set_value(fieldname, value) {
			this.doc[fieldname] = value;
		},
	};
	const field = {
		df: { label: "Customer" },
		inside_change_event: true,
		visibleValue: "Agenonga Congo",
		get_input_value() {
			return this.visibleValue;
		},
		get_search_args(query) {
			return { doctype: "Customer", txt: query };
		},
		async validate_link_and_fetch(value) {
			validationCalls.push(value);
			return value === "Agenonga  Congo" ? value : undefined;
		},
	};
	frm.fields_dict.customer = field;

	const resolveTypedLink = loadResolver({
		after_ajax: () => pendingAjax,
		async call() {
			return {
				message: [{ value: "Agenonga  Congo", label: "Agenonga Congo" }],
			};
		},
	});
	const resolution = resolveTypedLink(frm, "customer");

	await Promise.resolve();
	assert.deepEqual(validationCalls, []);

	// This is what the native blur validation does when the displayed label is
	// not the exact stored key: it finishes late and clears the control/model.
	field.inside_change_event = false;
	field.visibleValue = "";
	frm.doc.customer = undefined;
	releaseAjax();
	await resolution;

	assert.deepEqual(validationCalls, ["Agenonga Congo", "Agenonga  Congo"]);
	assert.equal(frm.doc.customer, "Agenonga  Congo");
});

test("uses the canonical value produced by native Link validation", async () => {
	let searches = 0;
	const frm = {
		doc: { customer: undefined },
		fields_dict: {},
		async set_value(fieldname, value) {
			this.doc[fieldname] = value;
		},
	};
	const field = {
		df: { label: "Customer" },
		inside_change_event: true,
		get_input_value: () => "Dorah Shop",
		validate_link_and_fetch: async () => {
			throw new Error("the settled value should not be revalidated");
		},
	};
	frm.fields_dict.customer = field;

	const resolveTypedLink = loadResolver({
		async after_ajax() {
			field.inside_change_event = false;
			frm.doc.customer = "Dorah Shop";
		},
		async call() {
			searches += 1;
			return { message: [] };
		},
	});

	await resolveTypedLink(frm, "customer");
	assert.equal(frm.doc.customer, "Dorah Shop");
	assert.equal(searches, 0);
});

test("waits for a pending native clear before checking mandatory fields", async () => {
	let releaseAjax;
	const pendingAjax = new Promise((resolve) => {
		releaseAjax = resolve;
	});
	const frm = {
		doc: { customer: "Previous Customer" },
		fields_dict: {},
	};
	const field = {
		df: { label: "Customer" },
		inside_change_event: true,
		get_input_value: () => "",
		validate_link_and_fetch: async () => {
			throw new Error("an empty Link should not be resolved again");
		},
	};
	frm.fields_dict.customer = field;

	const resolveTypedLink = loadResolver({ after_ajax: () => pendingAjax });
	const resolution = resolveTypedLink(frm, "customer");

	await Promise.resolve();
	assert.equal(frm.doc.customer, "Previous Customer");
	field.inside_change_event = false;
	frm.doc.customer = undefined;
	releaseAjax();
	await resolution;

	assert.equal(frm.doc.customer, undefined);
});
