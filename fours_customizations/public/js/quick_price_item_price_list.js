// fqp-1 Item Price list: a "Quick Price Update" button to the page. Extends ERPNext's list settings instead of replacing
// them; shown only when the user has the page (frappe.boot.page_info lists only the pages the user is allowed).
(function () {
	const settings = frappe.listview_settings["Item Price"] || {};
	const previous_onload = settings.onload;

	settings.onload = function (listview) {
		if (previous_onload) previous_onload.apply(this, arguments);
		if (!(frappe.boot.page_info || {})["quick-price-update"] || listview.page.fours_quick_price_button) return;
		listview.page.fours_quick_price_button = listview.page.add_inner_button(__("Quick Price Update"), () =>
			frappe.set_route("quick-price-update")
		);
	};
	frappe.listview_settings["Item Price"] = settings;
})();
