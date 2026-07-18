// "Print Payroll" button — shows in every document state (draft, submitted,
// cancelled) as long as the entry has salary slips. Offers the payroll sheet
// and the attendance-logs sheet, each as an inline PDF (printable) or an
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
					__("Payroll (PDF)"),
					() => download("download_payroll_pdf"),
					__("Print Payroll")
				);
				frm.add_custom_button(
					__("Payroll (Excel)"),
					() => download("download_payroll_excel"),
					__("Print Payroll")
				);
				frm.add_custom_button(
					__("Attendance Logs (PDF)"),
					() => download("download_attendance_logs_pdf"),
					__("Print Payroll")
				);
				frm.add_custom_button(
					__("Attendance Logs (Excel)"),
					() => download("download_attendance_logs_excel"),
					__("Print Payroll")
				);
			});
	},
});
