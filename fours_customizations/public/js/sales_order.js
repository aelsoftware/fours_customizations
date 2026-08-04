// Account Balance - ported from saleslive into fours_customizations.
// Fills the read-only `account_balance` field when a customer is picked.
frappe.ui.form.on('Sales Order', {
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
