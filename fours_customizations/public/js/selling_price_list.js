// Price list split (fours_customizations/price_lists.py): a new selling document whose price list is disabled (an
// archived list copied from an older document, or a desk default from before the split) switches to its company's own
// list. The value is set without the field's change event, so ERPNext does not re-price the lines: amended, returned
// and mapped lines keep the rates they came with. It runs in before_load, which Frappe awaits before it renders the form
// (ERPNext's refresh/link triggers reach apply_price_list before an onload handler's server call returns). The server
// does the same in before_validate. A disabled list that arrives later (another company, a customer's old default) is
// replaced before ERPNext's own selling_price_list handler prices the lines.
if (!frappe._fours_selling_price_list) {
	frappe._fours_selling_price_list = true;

	const replace_disabled_list = (frm) => {
		if (!frm.doc.__islocal || !frm.doc.selling_price_list || !frm.doc.company) {
			return;
		}
		const current = frm.doc.selling_price_list;
		return frappe
			.xcall("fours_customizations.price_lists.selling_list_replacement", {
				company: frm.doc.company,
				price_list: current,
			})
			.catch(() => null) // never stop ERPNext's own handlers that run after this one
			.then((replacement) => {
				if (replacement && frm.doc.selling_price_list === current) {
					frm.doc.selling_price_list = replacement;
					frm.refresh_field("selling_price_list");
					frappe.show_alert({
						message: __("Price list {0} is disabled: this document uses {1}.", [current, replacement]),
						indicator: "blue",
					});
				}
			});
	};

	["Quotation", "Sales Order", "Delivery Note", "Sales Invoice", "POS Invoice"].forEach((doctype) => {
		frappe.ui.form.on(doctype, {
			before_load: replace_disabled_list,
			company: replace_disabled_list,
			selling_price_list: replace_disabled_list,
		});
	});
}
