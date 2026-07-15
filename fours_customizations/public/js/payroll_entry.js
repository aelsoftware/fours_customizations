// "Print Payroll" button — shows once every salary slip of the entry has been
// cancelled, and offers the payroll sheet as an inline PDF (printable) or an
// Excel download.
frappe.ui.form.on("Payroll Entry", {
	refresh(frm) {
		if (frm.is_new()) return;

		frappe
			.call({
				method: "fours_customizations.payroll_print.get_print_allowed",
				args: { payroll_entry: frm.doc.name },
			})
			.then((r) => {
				if (!r.message || !r.message.allowed) return;

				const download = (method) => {
					const url = `/api/method/fours_customizations.payroll_print.${method}?payroll_entry=${encodeURIComponent(
						frm.doc.name
					)}`;
					window.open(url);
				};

				frm.add_custom_button(
					__("Print PDF"),
					() => download("download_payroll_pdf"),
					__("Print Payroll")
				);
				frm.add_custom_button(
					__("Export Excel"),
					() => download("download_payroll_excel"),
					__("Print Payroll")
				);
			});
	},
});
