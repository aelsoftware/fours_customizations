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
});
