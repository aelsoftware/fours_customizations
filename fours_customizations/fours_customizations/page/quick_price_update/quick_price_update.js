// Quick Price Update (fqp-1). Choose the company, see each of its selling lists side by side with the stock in its
// warehouses, edit the cells you may edit. UX only: fours_customizations.quick_price.api checks everything again and
// every save goes through Item Price (permissions, validations, Version) plus a Quick Price Change row.
const FQP_API = "fours_customizations.quick_price.api.";

frappe.pages["quick-price-update"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Quick Price Update"),
		single_column: true,
	});
	wrapper.quick_price_update = new FoursQuickPriceUpdate(page);
};

frappe.pages["quick-price-update"].on_page_show = function (wrapper) {
	const qpu = wrapper.quick_price_update;
	if (qpu && qpu.ready && !Object.keys(qpu.changes).length) qpu.refresh();
};

class FoursQuickPriceUpdate {
	constructor(page) {
		this.page = page;
		this.changes = {};
		this.extra_lists = [];
		this.start = 0;
		this.$body = $(`<div class="fqp"></div>`).appendTo(page.main);
		this.$lists = $(`<div class="fqp-lists" style="margin: 8px 0"></div>`).appendTo(this.$body);
		this.$table = $(`<div class="fqp-table" style="overflow-x: auto"></div>`).appendTo(this.$body);
		this.$pager = $(`<div class="fqp-pager" style="margin: 10px 0"></div>`).appendTo(this.$body);
		this.page.set_primary_action(__("Save Changes"), () => this.confirm_save());
		this.page.btn_primary.prop("disabled", true);
		this.load_context();
	}

	load_context() {
		frappe.xcall(FQP_API + "get_context").then((ctx) => {
			this.ctx = ctx;
			this.page_length = ctx.page_length || 100;
			if (!ctx.companies.length) {
				this.page.btn_primary.hide();
				this.$table.html(`<div class="text-muted" style="padding: 30px 0">${frappe.utils.escape_html(ctx.message)}</div>`);
				return;
			}
			this.make_filters();
			this.ready = true;
			this.refresh();
		});
	}

	make_filters() {
		this.company = this.page.add_field({
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Select",
			options: this.ctx.companies,
			default: this.ctx.default_company,
			change: () => this.on_filter_change(true),
		});
		this.search = this.page.add_field({
			fieldname: "search",
			label: __("Search item code, name or barcode"),
			fieldtype: "Data",
			change: frappe.utils.debounce(() => this.on_filter_change(), 300),
		});
		this.item_group = this.page.add_field({
			fieldname: "item_group",
			label: __("Item Group"),
			fieldtype: "Link",
			options: "Item Group",
			change: () => this.on_filter_change(),
		});
		this.only_unpriced = this.page.add_field({
			fieldname: "only_unpriced",
			label: __("Only without a price"),
			fieldtype: "Check",
			change: () => this.on_filter_change(),
		});
		this.all_sales_items = this.page.add_field({
			fieldname: "all_sales_items",
			label: __("All sales items"),
			fieldtype: "Check",
			change: () => this.on_filter_change(),
		});
		this.add_list = this.page.add_field({
			fieldname: "add_list",
			label: __("Add a price list"),
			fieldtype: "Select",
			options: [""].concat(this.ctx.selling_lists.map((l) => l.name)),
			change: () => {
				const name = this.add_list.get_value();
				if (!name) return;
				if (!this.extra_lists.includes(name)) this.extra_lists.push(name);
				this.add_list.set_value("");
				this.start = 0;
				this.refresh();
			},
		});
	}

	on_filter_change(company_changed) {
		const search = (this.search.get_value() || "").trim();
		if (search && search.length < 2) return;
		const pending = Object.keys(this.changes).length;
		if (pending && company_changed) frappe.msgprint(__("{0} unsaved changes were discarded.", [pending]));
		if (company_changed) this.changes = {};
		this.start = 0;
		this.refresh();
	}

	args() {
		return {
			company: this.company.get_value(),
			lists: JSON.stringify(this.extra_lists),
		};
	}

	refresh() {
		return frappe
			.xcall(FQP_API + "get_items", {
				...this.args(),
				search: this.search.get_value() || "",
				item_group: this.item_group.get_value() || "",
				only_unpriced: this.only_unpriced.get_value() ? 1 : 0,
				all_sales_items: this.all_sales_items.get_value() ? 1 : 0,
				start: this.start,
				page_length: this.page_length,
			})
			.then((data) => {
				this.data = data;
				this.render_lists(data.lists);
				this.render(data);
			});
	}

	render_lists(lists) {
		const esc = frappe.utils.escape_html;
		const shown = lists.map((l) => l.price_list);
		// Added lists the server did not accept (disabled, not selling, not permitted) are dropped from the request.
		this.extra_lists = this.extra_lists.filter((l) => shown.includes(l));
		this.$lists.html(
			lists
				.map((l) => {
					const remove = l.own ? "" : ` <a class="fqp-remove-list text-muted" data-list="${esc(l.price_list)}" title="${esc(__("Remove"))}">✕</a>`;
					const users = l.used_by.length ? `${__("Used by")}: ${l.used_by.map(esc).join(", ")}` : __("No company sold with it in the last 180 days");
					const notice = l.notice ? `<div class="text-warning small"><b>⚠ ${esc(l.notice)}</b></div>` : "";
					return `<div class="fqp-list" style="margin-bottom: 6px">
						<b>${esc(l.price_list)}</b> <span class="text-muted small">(${esc(l.currency || "")}) · ${users}</span>${remove}${notice}</div>`;
				})
				.join("") || `<div class="text-muted">${__("No selling price list for this company.")}</div>`
		);
		this.$lists.find(".fqp-remove-list").on("click", (e) => {
			const list = $(e.currentTarget).attr("data-list");
			this.extra_lists = this.extra_lists.filter((l) => l !== list);
			for (const key of Object.keys(this.changes)) if (this.changes[key].price_list === list) delete this.changes[key];
			this.refresh();
		});
	}

	key(item_code, price_list) {
		return `${item_code}\u0001${price_list}`;
	}

	render(data) {
		const esc = frappe.utils.escape_html;
		const lists = data.lists.map((l) => l.price_list);
		const head = [
			__("Item Code"),
			__("Item Name"),
			__("Group"),
			__("UOM"),
			...data.lists.map((l) => `${esc(l.price_list)}<br><span class="text-muted small">${esc(l.currency || "")}</span>`),
			...data.warehouses.map((w) => `<span class="small">${esc(w)}</span>`),
			__("Last Change"),
		];

		const rows = data.items.map((item) => {
			const also = Object.entries(item.also_sold_by || {})
				.map(([list, companies]) => __("also sold by {0} ({1})", [companies.map(esc).join(", "), esc(list)]))
				.join("<br>");
			const uom = item.uom === item.stock_uom ? esc(item.uom) : `${esc(item.uom)}<div class="text-muted">= ${item.factor} ${esc(item.stock_uom)}</div>`;
			const cells = [
				`<td>${esc(item.item_code)}</td>`,
				`<td>${esc(item.item_name || "")}${also ? `<div class="small text-muted fqp-also">${also}</div>` : ""}${
					item.warnings.length ? `<div class="small text-warning">${item.warnings.map(esc).join("<br>")}</div>` : ""
				}</td>`,
				`<td class="small">${esc(item.item_group || "")}</td>`,
				`<td class="small">${uom}</td>`,
			];
			for (const list of lists) {
				const cell = item.prices[list];
				const pending = this.changes[this.key(item.item_code, list)];
				const shown = pending ? pending.new_rate : cell.rate;
				const derived = cell.derived_from && !pending ? ` <span class="text-muted small" title="${esc(__("From the stock unit price"))}">≈</span>` : "";
				if (cell.editable) {
					cells.push(
						`<td style="min-width: 110px"><input type="number" min="0" step="any" class="form-control input-xs text-right fqp-rate"
							style="${pending ? "background: var(--yellow-highlight-color, #fff5c2)" : ""}" data-item="${esc(item.item_code)}" data-list="${esc(list)}"
							value="${shown === null || shown === undefined ? "" : shown}" placeholder="${esc(__("Add price"))}">${derived}</td>`
					);
				} else {
					cells.push(
						`<td class="text-right fqp-locked" title="${esc(cell.lock_reason || "")}">${
							cell.rate === null || cell.rate === undefined ? "—" : format_number(cell.rate, null, 2)
						} 🔒</td>`
					);
				}
			}
			for (const w of data.warehouses) {
				const qty = (item.stock || {})[w];
				cells.push(`<td class="text-right small">${qty ? format_number(qty, null, 2).replace(/\.00$/, "") : ""}</td>`);
			}
			const last = item.last_change;
			cells.push(`<td class="small text-muted">${last ? esc(last.by) + "<br>" + esc(frappe.datetime.str_to_user(last.at)) : ""}</td>`);
			return `<tr>${cells.join("")}</tr>`;
		});

		this.$table.html(
			data.items.length
				? `<table class="table table-bordered table-condensed" style="background: var(--card-bg)">
					<thead><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr></thead>
					<tbody>${rows.join("")}</tbody></table>`
				: `<div class="text-muted" style="padding: 30px 0">${
						this.all_sales_items.get_value()
							? __("No items match.")
							: __("No item this company sells or stocks matches. Tick 'All sales items' to search every sales item.")
					}</div>`
		);
		this.$table.find(".fqp-rate").on("change", (e) => this.on_rate_change($(e.target)));
		this.render_pager(data);
		this.update_save_button();
	}

	render_pager(data) {
		this.$pager.empty();
		if (this.start > 0) {
			$(`<button class="btn btn-default btn-xs">${__("Previous")}</button>`)
				.appendTo(this.$pager)
				.on("click", () => this.go(Math.max(0, this.start - this.page_length)));
		}
		if (data.has_more) {
			$(`<button class="btn btn-default btn-xs" style="margin-left: 6px">${__("Next")}</button>`)
				.appendTo(this.$pager)
				.on("click", () => this.go(this.start + this.page_length));
		}
	}

	go(start) {
		const pending = Object.keys(this.changes).length;
		if (pending) {
			frappe.msgprint(__("Save or undo your {0} changes before moving to another page.", [pending]));
			return;
		}
		this.start = start;
		this.refresh();
	}

	on_rate_change($input) {
		const item_code = $input.attr("data-item");
		const list = $input.attr("data-list");
		const item = this.data.items.find((i) => i.item_code === item_code);
		const cell = item.prices[list];
		const value = $input.val() === "" ? null : flt($input.val());
		const key = this.key(item_code, list);
		if (value === null || (value === cell.rate && cell.item_price)) {
			delete this.changes[key];
			$input.css("background", "");
		} else {
			this.changes[key] = {
				item_code,
				item_name: item.item_name,
				price_list: list,
				uom: cell.record_uom || item.uom,
				item_price: cell.item_price || null,
				expected_old_rate: cell.rate,
				new_rate: value,
			};
			$input.css("background", "var(--yellow-highlight-color, #fff5c2)");
		}
		this.update_save_button();
	}

	update_save_button() {
		const n = Object.keys(this.changes).length;
		// Only the label changes: re-setting the action would unbind the click while the button is being clicked (the
		// edited cell's change event fires on the same mousedown).
		const label = n === 1 ? __("Save 1 Change") : n ? __("Save {0} Changes", [n]) : __("Save Changes");
		this.page.btn_primary.text(label).attr("data-label", label).prop("disabled", !n);
	}

	confirm_save() {
		const changes = Object.values(this.changes);
		if (!changes.length) return;
		const max = this.ctx.max_changes || 100;
		if (changes.length > max) {
			frappe.msgprint(__("At most {0} price changes can be saved at once: you have {1}.", [max, changes.length]));
			return;
		}
		const large_pct = this.ctx.large_change_pct || 30;
		const esc = frappe.utils.escape_html;
		const pct = (c) => (c.expected_old_rate ? flt(((c.new_rate - c.expected_old_rate) / c.expected_old_rate) * 100, 6) : null);
		// as the server: against the price a new invoice fetches today (also a price derived from the stock unit)
		const large = changes.filter((c) => pct(c) !== null && Math.abs(pct(c)) > large_pct);
		const shared = this.data.lists.filter((l) => l.notice && changes.some((c) => c.price_list === l.price_list));
		const lines = changes
			.map((c) => {
				const old = c.expected_old_rate ? format_number(c.expected_old_rate, null, 2) : __("no price");
				const p = (pct(c) !== null ? ` (${flt(pct(c), 1)}%)` : "") + (c.item_price ? "" : ` (${__("new record")})`);
				const warn = large.includes(c) ? ' style="color: var(--red-500, #e24c4c)"' : "";
				return `<tr${warn}><td>${esc(c.item_code)}</td><td>${esc(c.price_list)}</td><td class="text-right">${old}</td>
					<td class="text-right"><b>${format_number(c.new_rate, null, 2)}</b>${p}</td></tr>`;
			})
			.join("");
		const notices = shared.map((l) => `<div class="text-warning"><b>⚠ ${esc(l.notice)}</b></div>`).join("");
		const dialog = new frappe.ui.Dialog({
			title: changes.length === 1 ? __("Save 1 price change") : __("Save {0} price changes", [changes.length]),
			size: "large",
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "summary",
					options: `${notices}<table class="table table-condensed"><thead><tr><th>${__("Item")}</th><th>${__("List")}</th>
						<th class="text-right">${__("Old")}</th><th class="text-right">${__("New")}</th></tr></thead><tbody>${lines}</tbody></table>`,
				},
				{ fieldtype: "Small Text", fieldname: "reason", label: __("Reason (optional)") },
				{
					fieldtype: "Check",
					fieldname: "confirm_large",
					label: __("I confirm the {0} changes of more than {1}% (in red)", [large.length, large_pct]),
					hidden: large.length ? 0 : 1,
				},
			],
			primary_action_label: __("Save"),
			primary_action: (values) => {
				if (large.length && !values.confirm_large) {
					frappe.msgprint(__("Tick the confirmation for changes of more than {0}%.", [large_pct]));
					return;
				}
				dialog.hide();
				this.save(changes, values.reason, values.confirm_large ? 1 : 0);
			},
		});
		dialog.show();
	}

	save(changes, reason, confirm_large) {
		frappe
			.xcall(
				FQP_API + "save_prices",
				{ ...this.args(), changes, reason: reason || "", confirm_large },
				"POST",
				{ freeze: true, freeze_message: __("Saving prices...") }
			)
			.then((results) => {
				const esc = frappe.utils.escape_html;
				const failed = results.filter((r) => !r.ok);
				// Refused changes are listed, not kept: the reloaded grid shows the current prices to start again from.
				this.changes = {};
				const saved = results.length - failed.length;
				if (failed.length) {
					frappe.msgprint({
						title: __("{0} saved, {1} not saved", [saved, failed.length]),
						indicator: "orange",
						message: failed
							.map((r) => `<b>${esc(r.item_code || "")}</b> · ${esc(r.price_list || "")}: ${esc(r.message || "")}`)
							.join("<br>"),
					});
				} else {
					frappe.show_alert({ message: __("{0} prices saved", [saved]), indicator: "green" });
				}
				this.refresh();
			});
	}
}
