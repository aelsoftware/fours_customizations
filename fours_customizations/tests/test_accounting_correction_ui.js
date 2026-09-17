"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SCRIPT_PATH = path.resolve(__dirname, "../public/js/accounting_correction.js");
const SCRIPT = fs.readFileSync(SCRIPT_PATH, "utf8");

const COMPANY = "4S Industries Limited";
const ROLE = "4S Accountant";
const EXPECTED_MODIFIED = "2026-09-17 10:00:00.000000";
const REASON = "Correcting the reviewed accounting record";

function plain(value) {
	return JSON.parse(JSON.stringify(value));
}

async function loadDialog({ action, doctype, context: contextOverrides = {} }) {
	const handlers = {};
	const buttons = [];
	const dialogs = [];
	const requests = [];
	const alerts = [];
	const routes = [];
	let reloads = 0;

	const context = {
		doctype,
		name: `TEST-${doctype === "Sales Invoice" ? "SI" : "PE"}`,
		company: COMPANY,
		posting_date: "2026-09-16",
		modified: EXPECTED_MODIFIED,
		is_pos: doctype === "Sales Invoice" ? 1 : 0,
		update_stock: 0,
		mode_of_payment: "Cash Ugx",
		payment_type: doctype === "Payment Entry" ? "Receive" : null,
		paid_from: "Debtors - 4S",
		paid_to: "1110 - Cash - 4S",
		reference_no: "OLD-REF",
		reference_date: "2026-09-16",
		payment_rows:
			doctype === "Sales Invoice"
				? [
						{
							name: "PAY-ROW-1",
							mode_of_payment: "Cash Ugx",
							account: "1110 - Cash - 4S",
							amount: 1000000,
						},
						{
							name: "PAY-ROW-2",
							mode_of_payment: "MTN Mobile Money",
							account: "MTN Mobile Money - 4S",
							amount: 280000,
						},
					]
				: [],
		include_amount: 1280000,
		currency: "UGX",
		modes_of_payment: [
			{ name: "Cash Ugx", account: "1110 - Cash - 4S", account_currency: "UGX" },
			{
				name: "MTN Mobile Money",
				account: "MTN Mobile Money - 4S",
				account_currency: "UGX",
			},
		],
		available_actions: {
			change_posting_date: false,
			exclude_payment: false,
			include_payment: false,
			change_mode_of_payment: false,
			[action]: true,
		},
		used_actions: {},
		...contextOverrides,
	};

	class Dialog {
		constructor(options) {
			this.options = options;
			this.shown = 0;
			this.hidden = 0;
			this.disabled = 0;
			this.enabled = 0;
			dialogs.push(this);
		}

		show() {
			this.shown += 1;
		}

		hide() {
			this.hidden += 1;
		}

		disable_primary_action() {
			this.disabled += 1;
		}

		enable_primary_action() {
			this.enabled += 1;
		}
	}

	const frappe = {
		four_s_accounting_correction: {},
		user_roles: [ROLE],
		ui: {
			form: {
				on(registeredDoctype, events) {
					handlers[registeredDoctype] = events;
				},
			},
			Dialog,
		},
		async xcall(method, args) {
			assert.equal(
				method,
				"fours_customizations.accounting_correction.get_correction_context",
			);
			assert.deepEqual(plain(args), { doctype, name: context.name });
			return context;
		},
		async call(request) {
			requests.push(plain(request));
			return { message: { message: "Correction applied" } };
		},
		show_alert(options, seconds) {
			alerts.push({ options: plain(options), seconds });
		},
		set_route(...parts) {
			routes.push(plain(parts));
		},
	};

	vm.runInNewContext(
		SCRIPT,
		{
			frappe,
			__: (message) => message,
			flt: (value) => Number(value || 0),
		},
		{ filename: SCRIPT_PATH },
	);

	const frm = {
		doctype,
		doc: {
			name: context.name,
			docstatus: 1,
			company: COMPANY,
		},
		add_custom_button(label, callback, group) {
			buttons.push({ label, callback, group });
		},
		async reload_doc() {
			reloads += 1;
		},
	};

	handlers[doctype].refresh(frm);
	await new Promise((resolve) => setImmediate(resolve));

	const labelByAction = {
		change_posting_date: "Change Posting Date",
		exclude_payment: "Exclude Payment",
		include_payment: "Include Payment",
		change_mode_of_payment: "Change Mode of Payment",
	};
	const actionButton = buttons.find((button) => button.label === labelByAction[action]);
	assert.ok(actionButton, `${action} button was not added`);
	assert.equal(actionButton.group, "Accounting Corrections");
	actionButton.callback();
	assert.equal(dialogs.length, 1);

	return {
		alerts,
		context,
		dialog: dialogs[0],
		getReloads: () => reloads,
		requests,
		routes,
	};
}

const cases = [
	{
		name: "posting date",
		action: "change_posting_date",
		doctype: "Sales Invoice",
		values: { new_posting_date: "2026-09-17", reason: REASON },
		extraArgs: { new_posting_date: "2026-09-17" },
		fieldAssertions(fields) {
			const field = fields.find((candidate) => candidate.fieldname === "new_posting_date");
			assert.deepEqual(plain(field), {
				fieldtype: "Date",
				fieldname: "new_posting_date",
				label: "New Posting Date",
				default: "2026-09-16",
				reqd: 1,
			});
		},
	},
	{
		name: "exclude payment",
		action: "exclude_payment",
		doctype: "Sales Invoice",
		values: { recorded_payment: 1, reason: REASON },
		extraArgs: {},
		fieldAssertions(fields) {
			const field = fields.find((candidate) => candidate.fieldname === "recorded_payment");
			assert.equal(field.default, 1280000);
			assert.equal(field.read_only, 1);
		},
	},
	{
		name: "include payment",
		action: "include_payment",
		doctype: "Sales Invoice",
		context: { is_pos: 0, payment_rows: [] },
		values: {
			amount_to_include: 1,
			new_mode_of_payment: "MTN Mobile Money",
			reason: REASON,
		},
		extraArgs: { new_mode_of_payment: "MTN Mobile Money" },
		fieldAssertions(fields) {
			const amount = fields.find((candidate) => candidate.fieldname === "amount_to_include");
			assert.equal(amount.default, 1280000);
			assert.equal(amount.read_only, 1);
			assert.equal(
				fields.find((candidate) => candidate.fieldname === "new_mode_of_payment").reqd,
				1,
			);
		},
	},
	{
		name: "sales invoice payment split",
		action: "change_mode_of_payment",
		doctype: "Sales Invoice",
		values: {
			locked_paid_amount: 1,
			payment_allocations: [
				{
					idx: 1,
					mode_of_payment: "Cash Ugx",
					account: "tampered account",
					amount: 1000000,
					base_amount: 10,
				},
				{
					idx: 2,
					mode_of_payment: "MTN Mobile Money",
					amount: 280000,
				},
			],
			reason: REASON,
		},
		extraArgs: {
			payment_allocations: [
				{ mode_of_payment: "Cash Ugx", amount: 1000000 },
				{ mode_of_payment: "MTN Mobile Money", amount: 280000 },
			],
		},
		fieldAssertions(fields) {
			const locked = fields.find(
				(candidate) => candidate.fieldname === "locked_paid_amount",
			);
			assert.equal(locked.default, 1280000);
			assert.equal(locked.read_only, 1);
			const allocations = fields.find(
				(candidate) => candidate.fieldname === "payment_allocations",
			);
			assert.equal(allocations.reqd, 1);
			assert.deepEqual(plain(allocations.data), [
				{ mode_of_payment: "Cash Ugx", amount: 1000000 },
				{ mode_of_payment: "MTN Mobile Money", amount: 280000 },
			]);
		},
	},
	{
		name: "payment entry mode",
		action: "change_mode_of_payment",
		doctype: "Payment Entry",
		values: {
			new_mode_of_payment: "MTN Mobile Money",
			new_reference_no: "NEW-REF",
			new_reference_date: "2026-09-17",
			reason: REASON,
		},
		extraArgs: {
			new_mode_of_payment: "MTN Mobile Money",
			new_reference_no: "NEW-REF",
			new_reference_date: "2026-09-17",
		},
	},
	{
		name: "internal transfer mode",
		action: "change_mode_of_payment",
		doctype: "Payment Entry",
		context: { payment_type: "Internal Transfer" },
		values: {
			new_mode_of_payment: "MTN Mobile Money",
			new_reference_no: "TRANSFER-REF",
			new_reference_date: "2026-09-17",
			reason: REASON,
		},
		extraArgs: {
			new_mode_of_payment: "MTN Mobile Money",
			account_side: "paid_from",
			new_reference_no: "TRANSFER-REF",
			new_reference_date: "2026-09-17",
		},
		fieldAssertions(fields) {
			const field = fields.find(
				(candidate) => candidate.fieldname === "new_mode_of_payment",
			);
			assert.match(field.description, /controls Account Paid From/);
		},
	},
];

test("correction dialogs send only the action-specific server contract", async (t) => {
	for (const definition of cases) {
		await t.test(definition.name, async () => {
			const harness = await loadDialog({
				action: definition.action,
				doctype: definition.doctype,
				context: definition.context,
			});
			const fields = harness.dialog.options.fields;
			const reason = fields.find((candidate) => candidate.fieldname === "reason");
			const warning = fields.find((candidate) => candidate.fieldname === "one_time_warning");
			assert.equal(reason.reqd, 1);
			assert.match(reason.description, /permanent audit log/);
			assert.match(warning.options, /only once/);
			definition.fieldAssertions?.(fields);

			await harness.dialog.options.primary_action(definition.values);

			assert.equal(harness.requests.length, 1);
			assert.deepEqual(harness.requests[0], {
				method: "fours_customizations.accounting_correction.apply_correction",
				type: "POST",
				args: {
					doctype: definition.doctype,
					name: harness.context.name,
					action: definition.action,
					reason: REASON,
					expected_modified: EXPECTED_MODIFIED,
					...definition.extraArgs,
				},
				freeze: true,
				freeze_message: "Updating the document and ledgers...",
			});
			assert.equal(harness.dialog.shown, 1);
			assert.equal(harness.dialog.disabled, 1);
			assert.equal(harness.dialog.hidden, 1);
			assert.equal(harness.dialog.enabled, 1);
			assert.equal(harness.getReloads(), 1);
			assert.deepEqual(harness.alerts, [
				{
					options: { message: "Correction applied", indicator: "green" },
					seconds: 7,
				},
			]);
		});
	}
});
