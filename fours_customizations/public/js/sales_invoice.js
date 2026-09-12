frappe.ui.form.on("Sales Invoice", {
	is_pos(frm) {
		frappe.db
			.get_value("Company", frm.doc.company, "enable_selling_automations")
			.then(({ message }) => {
				if (message && message.enable_selling_automations && frm.doc.is_pos) {
					frm.set_value("update_stock", 0);
				}
			});
	},
	async validate(frm) {
		if (frm.doc.company !== "4S Industries Limited") return;

		// A Link control updates the document only after its async validation has
		// finished. If a user types an exact value and immediately clicks Save,
		// Frappe can run the mandatory check while the value is still only visible
		// in the input. Resolve those two required 4S links before that check.
		await resolve_typed_link(frm, "customer");
		await resolve_typed_link(frm, "custom_sales_person");

		if (!frm.doc.custom_sales_person) {
			const people = [
				...new Set(
					(frm.doc.sales_team || [])
						.map((row) => row.sales_person)
						.filter(Boolean),
				),
			];

			if (people.length === 1) {
				await frm.set_value("custom_sales_person", people[0]);
			} else if (people.length > 1) {
				frappe.throw(
					__("Select one primary Sales Person. The Sales Team contains more than one person."),
				);
			}
		}

		if (!frm.doc.custom_sales_person) {
			frappe.throw(
				__("Sales Person is required. Select it above or add one person to Sales Team."),
			);
		}
	},
});

async function resolve_typed_link(frm, fieldname) {
	if (frm.doc[fieldname]) return;

	const field = frm.fields_dict[fieldname];
	const typed_value = field?.get_input_value?.()?.trim();
	if (!typed_value || !field.validate_link_and_fetch) return;

	const resolved_value = await field.validate_link_and_fetch(typed_value);
	if (resolved_value) {
		await frm.set_value(fieldname, resolved_value);
		return;
	}

	const label = __(field.df.label || fieldname);
	frappe.throw(
		__(
			"{0} <b>{1}</b> was typed but not selected. Choose an existing {0} from the dropdown, or create it first if it is new.",
			[label, frappe.utils.escape_html(typed_value)],
		),
	);
}

// Adjust Price — correct a price after the Delivery Note has gone out.
// Cancelling a delivered invoice is blocked (sales_chain_integrity), so this
// raises a Credit Note or a supplementary invoice for the difference instead.
frappe.ui.form.on("Sales Invoice", {
	refresh(frm) {
		if (frm.doc.docstatus !== 1 || frm.doc.is_return || frm.doc.is_consolidated) {
			return;
		}
		frm.add_custom_button(__("Adjust Price"), () => open_price_adjustment(frm), __("Corrections"));
	},
});

function open_price_adjustment(frm) {
	frappe.call({
		method: "fours_customizations.price_adjustment.get_adjustment_context",
		args: { sales_invoice: frm.doc.name },
		freeze: true,
		freeze_message: __("Loading prices..."),
		callback: ({ message: ctx }) => {
			if (!ctx) return;
			if (!ctx.enabled) {
				frappe.msgprint(__("Price adjustment is switched off in Four S Industries Settings."));
				return;
			}
			show_price_dialog(frm, ctx);
		},
	});
}

function show_price_dialog(frm, ctx) {
	const limits = ctx.limits || {};
	const bullets = [];
	if (limits.max_age_days)
		bullets.push(__("invoice older than {0} days", [limits.max_age_days]));
	if (limits.max_amount)
		bullets.push(__("more than {0}", [format_currency(limits.max_amount, ctx.currency)]));
	if (limits.max_percent)
		bullets.push(__("more than {0}% of the invoice", [limits.max_percent]));
	if (limits.block_below_cost) bullets.push(__("a price below the buying price"));
	if (limits.block_net_credit) bullets.push(__("leaving the customer in credit"));

	const note = ctx.is_approver
		? __("You hold the {0} role, so you may exceed the limits.", [ctx.approver_role])
		: __("Needs {0}: {1}.", [ctx.approver_role || __("an approver"), bullets.join(", ")]);

	const dialog = new frappe.ui.Dialog({
		title: __("Adjust Price — {0}", [frm.doc.name]),
		size: "large",
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "intro",
				options: `<p style="margin-bottom:8px;color:#6c7680;">
					${__("The delivery stands. Changing a price here raises a Credit Note (if you lower it) or a supplementary invoice (if you raise it) for the difference only.")}
					<br><b>${frappe.utils.escape_html(note)}</b></p>`,
			},
			{
				fieldtype: "Table",
				fieldname: "rows",
				cannot_add_rows: true,
				cannot_delete_rows: true,
				in_place_edit: false,
				data: ctx.items.map((it) => ({ ...it, new_rate: it.current_rate })),
				get_data: () => ctx.items.map((it) => ({ ...it, new_rate: it.current_rate })),
				fields: [
					{ fieldtype: "Data", fieldname: "item_row", hidden: 1 },
					{ fieldtype: "Data", fieldname: "item_code", label: __("Item"), in_list_view: 1, read_only: 1, columns: 3 },
					{ fieldtype: "Float", fieldname: "qty", label: __("Qty"), in_list_view: 1, read_only: 1, columns: 1 },
					{ fieldtype: "Currency", fieldname: "current_rate", label: __("Current"), in_list_view: 1, read_only: 1, columns: 2 },
					{ fieldtype: "Currency", fieldname: "new_rate", label: __("New Rate"), in_list_view: 1, reqd: 1, columns: 2 },
					{ fieldtype: "Currency", fieldname: "buying_rate", label: __("Buying"), in_list_view: 1, read_only: 1, columns: 2 },
				],
			},
			{
				fieldtype: "Small Text",
				fieldname: "reason",
				label: __("Reason for the price change"),
				reqd: 1,
				description: __("At least {0} characters. This is recorded on the correction and posted to Slack.", [ctx.min_reason_length]),
			},
		],
		primary_action_label: __("Create Correction"),
		primary_action(values) {
			const changed = (values.rows || [])
				.filter((r) => flt(r.new_rate) !== flt(r.current_rate))
				.map((r) => ({ item_row: r.item_row, new_rate: flt(r.new_rate) }));

			if (!changed.length) {
				frappe.msgprint(__("No price was changed."));
				return;
			}
			frappe.call({
				method: "fours_customizations.price_adjustment.create_price_adjustment",
				args: { sales_invoice: frm.doc.name, rows: changed, reason: values.reason },
				freeze: true,
				freeze_message: __("Creating the correction..."),
				callback: ({ message: res }) => {
					if (!res) return;
					dialog.hide();
					frappe.show_alert({
						message: __("{0} created", [res.name]),
						indicator: "green",
					});
					frappe.set_route("Form", "Sales Invoice", res.name);
				},
			});
		},
	});
	dialog.show();
}

// Account Balance - ported from saleslive into fours_customizations.
// Fills the read-only `account_balance` field when a customer is picked.
frappe.ui.form.on('Sales Invoice', {
	customer: function (frm) {
		if (!frm.doc.customer) {
			frm.set_value('account_balance', 'Select Customer First');
			return;
		}
		frappe.call({
			method: 'fours_customizations.customer_balance.customer',
			args: { customer: frm.doc.customer, company: frm.doc.company },
			callback: function (r) {
				if (r.message) {
					frm.set_value('account_balance', r.message.formatted);
				}
			},
		});
	},
});
