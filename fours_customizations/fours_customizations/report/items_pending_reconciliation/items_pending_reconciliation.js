frappe.query_reports["Items Pending Reconciliation"] = {
	filters: [],
	onload: function (report) {
		report.page.add_inner_button(__("Reconcile All"), function () {
			const data = report.data || [];
			if (!data.length) {
				frappe.msgprint(__("Nothing to reconcile."));
				return;
			}
			const items = [...new Set(data.map((row) => row.item_code).filter(Boolean))];
			frappe.confirm(
				__("Create a draft Stock Reconciliation for {0} items?", [items.length]),
				function () {
					frappe.call({
						method: "fours_customizations.negative_stock_handler.reconcile_items",
						args: { item_codes: items },
						freeze: true,
						freeze_message: __("Creating Stock Reconciliation..."),
						callback: function (r) {
							if (r.message) {
								frappe.set_route("Form", "Stock Reconciliation", r.message);
							}
						},
					});
				}
			);
		});
	},
};
