(() => {
	const controls = (frappe.four_s_accounting_correction ||= {});
	if (controls.registered) return;
	controls.registered = true;

	const COMPANY = "4S Industries Limited";
	const ROLE = "4S Accountant";
	const AUDIT_DOCTYPE = "Four S Accounting Correction";
	const GROUP = __("Accounting Corrections");
	const METHOD = "fours_customizations.accounting_correction";

	const ACTIONS = {
		change_posting_date: __("Change Posting Date"),
		exclude_payment: __("Exclude Payment"),
		include_payment: __("Include Payment"),
		change_mode_of_payment: __("Change Mode of Payment"),
	};

	for (const doctype of ["Sales Invoice", "Payment Entry"]) {
		frappe.ui.form.on(doctype, {
			refresh(frm) {
				add_correction_buttons(frm);
			},
		});
	}

	async function add_correction_buttons(frm) {
		if (
			frm.doc.docstatus !== 1 ||
			frm.doc.company !== COMPANY ||
			!frappe.user_roles.includes(ROLE)
		) {
			return;
		}

		const document_name = frm.doc.name;
		const context = await frappe.xcall(`${METHOD}.get_correction_context`, {
			doctype: frm.doctype,
			name: document_name,
		});
		if (frm.doc.name !== document_name || frm.doc.docstatus !== 1) return;

		for (const [action, label] of Object.entries(ACTIONS)) {
			if (!context.available_actions[action]) continue;
			frm.add_custom_button(label, () => show_correction_dialog(frm, context, action), GROUP);
		}

		frm.add_custom_button(
			__("View Correction History"),
			() =>
				frappe.set_route("List", AUDIT_DOCTYPE, {
					reference_doctype: frm.doctype,
					reference_name: frm.doc.name,
				}),
			GROUP,
		);
	}

	function show_correction_dialog(frm, context, action) {
		const fields = correction_fields(context, action);
		fields.push({
			fieldtype: "Small Text",
			fieldname: "reason",
			label: __("Reason for Correction"),
			reqd: 1,
			description: __("Enter at least 10 characters. This becomes part of the permanent audit log."),
		});
		const warning = __(
			"This action can be used only once on this document. The submitted document and its ledgers will be updated without cancellation or amendment.",
		);
		fields.push({
			fieldtype: "HTML",
			fieldname: "one_time_warning",
			options: `<div class="alert alert-warning" style="margin-bottom:0">
				${warning}
			</div>`,
		});

		const dialog = new frappe.ui.Dialog({
			title: `${ACTIONS[action]} — ${frm.doc.name}`,
			fields,
			primary_action_label: __("Apply Once"),
			async primary_action(values) {
				dialog.disable_primary_action();
				try {
					const args = {
						doctype: frm.doctype,
						name: frm.doc.name,
						action,
						reason: values.reason,
						expected_modified: context.modified,
					};
					add_action_values(args, values, context, action);
					const { message } = await frappe.call({
						method: `${METHOD}.apply_correction`,
						type: "POST",
						args,
						freeze: true,
						freeze_message: __("Updating the document and ledgers..."),
					});
					dialog.hide();
					frappe.show_alert({ message: message.message, indicator: "green" }, 7);
					await frm.reload_doc();
				} finally {
					dialog.enable_primary_action();
				}
			},
		});
		dialog.show();
	}

	function correction_fields(context, action) {
		if (action === "change_posting_date") {
			return [
				{
					fieldtype: "Date",
					fieldname: "new_posting_date",
					label: __("New Posting Date"),
					default: context.posting_date,
					reqd: 1,
				},
			];
		}

		if (action === "exclude_payment") {
			return [
				{
					fieldtype: "Currency",
					fieldname: "recorded_payment",
					label: __("Payment Currently Included"),
					default: sum_payment_rows(context.payment_rows),
					options: context.currency,
					read_only: 1,
				},
			];
		}

		if (action === "include_payment") {
			return [
				{
					fieldtype: "Currency",
					fieldname: "amount_to_include",
					label: __("Outstanding Amount to Include"),
					default: context.include_amount,
					options: context.currency,
					read_only: 1,
				},
				mode_of_payment_field(context),
			];
		}

		const fields = [];
		if (context.doctype === "Sales Invoice") {
			fields.push({
				fieldtype: "Currency",
				fieldname: "locked_paid_amount",
				label: __("Locked Paid Amount"),
				default: sum_payment_rows(context.payment_rows),
				options: context.currency,
				read_only: 1,
			});
			fields.push(invoice_payment_allocations_field(context));
		} else {
			fields.push(mode_of_payment_field(context));
			fields.push(
				{
					fieldtype: "Data",
					fieldname: "new_reference_no",
					label: __("Bank Reference No"),
					default: context.reference_no,
					description: __(
						"Required for a Bank account. These bank-reference fields are cleared when the corrected account is Cash.",
					),
				},
				{
					fieldtype: "Date",
					fieldname: "new_reference_date",
					label: __("Bank Reference Date"),
					default: context.reference_date,
					description: __(
						"Required for a Bank account. These bank-reference fields are cleared when the corrected account is Cash.",
					),
				},
			);
		}
		return fields;
	}

	function invoice_payment_allocations_field(context) {
		return {
			fieldtype: "Table",
			fieldname: "payment_allocations",
			label: __("Payment Mode Allocation"),
			data: context.payment_rows.map((row) => ({
				mode_of_payment: row.mode_of_payment,
				amount: row.amount,
			})),
			in_place_edit: true,
			reqd: 1,
			description: __("You may split the receipt across modes, but the rows must total the locked paid amount."),
			fields: [
				{
					fieldtype: "Select",
					fieldname: "mode_of_payment",
					label: __("Mode of Payment"),
					options: mode_options(context),
					in_list_view: 1,
					reqd: 1,
					columns: 6,
				},
				{
					fieldtype: "Currency",
					fieldname: "amount",
					label: __("Allocation"),
					options: context.currency,
					in_list_view: 1,
					reqd: 1,
					columns: 4,
				},
			],
		};
	}

	function mode_of_payment_field(context) {
		const field = {
			fieldtype: "Select",
			fieldname: "new_mode_of_payment",
			label: __("New Mode of Payment"),
			options: mode_options(context, true),
			reqd: 1,
		};
		if (context.payment_type === "Internal Transfer") {
			field.description = __(
				"For an Internal Transfer, Mode of Payment controls Account Paid From.",
			);
		}
		return field;
	}

	function mode_options(context, blank = false) {
		const options = context.modes_of_payment.map((mode) => ({
			value: mode.name,
			label: `${mode.name} — ${mode.account} (${mode.account_currency})`,
		}));
		return blank ? [{ value: "", label: "" }, ...options] : options;
	}

	function add_action_values(args, values, context, action) {
		if (action === "change_posting_date") {
			args.new_posting_date = values.new_posting_date;
		}
		if (
			action === "include_payment" ||
			(action === "change_mode_of_payment" && context.doctype === "Payment Entry")
		) {
			args.new_mode_of_payment = values.new_mode_of_payment;
		}
		if (action === "change_mode_of_payment" && context.doctype === "Sales Invoice") {
			args.payment_allocations = (values.payment_allocations || []).map((row) => ({
				mode_of_payment: row.mode_of_payment,
				amount: row.amount,
			}));
		}
		if (action === "change_mode_of_payment" && context.payment_type === "Internal Transfer") {
			args.account_side = "paid_from";
		}
		if (action === "change_mode_of_payment" && context.doctype === "Payment Entry") {
			args.new_reference_no = values.new_reference_no;
			args.new_reference_date = values.new_reference_date;
		}
	}

	function sum_payment_rows(rows) {
		return (rows || []).reduce((total, row) => total + flt(row.amount), 0);
	}
})();
