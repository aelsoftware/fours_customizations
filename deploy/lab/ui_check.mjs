// fqp-1 LAB ONLY: headless Chromium check of Quick Price Update on the private lab bench (run in kasc-bom-tills):
//   node ui_check.mjs <passwords.json> <screenshot dir>
// Base http://10.67.210.10:18223 (deploy/lab/serve.sh, multax stand-in fqpm.lab). Every request to another host is
// aborted and counted. Prints one JSON line per user; exit 1 when an expectation fails.
import fs from "node:fs";
import { chromium } from "/opt/kr-playwright/node_modules/playwright/index.mjs";

const BASE = "http://10.67.210.10:18223";
const [pwFile, shots] = process.argv.slice(2);
const passwords = JSON.parse(fs.readFileSync(pwFile, "utf8"));
const USERS = [
	{ email: "fqp.writer@labvox.invalid", company: "Vox Lounge Nansana", page: true, edits: true },
	{ email: "fqp.reader@labvox.invalid", company: "4S Industries Limited", page: true, edits: false },
	{ email: "fqp.tv@labvox.invalid", company: null, page: false, edits: false },
];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await chromium.launch({ executablePath: "/opt/ms-playwright/chromium-1243/chrome-linux64/chrome" });
let failed = 0;
for (const u of USERS) {
	const out = { user: u.email.split("@")[0], blocked: 0, console_errors: [] };
	const fail = (what) => {
		out.failures = (out.failures || []).concat(what);
		failed++;
	};
	const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
	await ctx.route("**/*", (route) => {
		if (new URL(route.request().url()).host === "10.67.210.10:18223") return route.continue();
		out.blocked++;
		return route.abort();
	});
	const login = await ctx.request.post(`${BASE}/api/method/login`, { form: { usr: u.email, pwd: passwords[u.email] } });
	out.login = login.status();
	const page = await ctx.newPage();
	try {
	page.on("console", (m) => m.type() === "error" && out.console_errors.push(m.text().slice(0, 160)));
	page.on("pageerror", (e) => out.console_errors.push("pageerror: " + String(e.stack || e).slice(0, 400)));

	// 1. Item Price list: the button only for users with the page
	await page.goto(`${BASE}/desk/item-price`, { waitUntil: "networkidle" });
	await sleep(1500);
	out.list_button = await page.locator('button:has-text("Quick Price Update")').count();
	if (!!out.list_button !== u.page) fail("list button");
	await page.screenshot({ path: `${shots}/${out.user}-item-price-list.png` });

	// 2. desk search: the page is offered only to users with the page
	out.search = await page.evaluate(() => {
		const info = (frappe.boot.page_info || {})["quick-price-update"];
		let found = [];
		try {
			found = (frappe.search.utils.get_pages("quick price") || []).map((r) => r.label || r.value);
		} catch (e) {
			found = ["error " + e];
		}
		return { page_info: !!info, results: found.map((x) => String(x).replace(/<[^>]+>/g, "")) };
	});
	if (out.search.page_info !== u.page) fail("page_info");

	// 3. the page
	await page.goto(`${BASE}/desk/quick-price-update`, { waitUntil: "networkidle" });
	await sleep(2500);
	if (u.page) {
		const company = page.locator('.page-form select[data-fieldname="company"]');
		if (u.company && (await company.count())) {
			await company.selectOption(u.company);
			await page.waitForLoadState("networkidle");
			await sleep(2500);
		}
		out.companies = await company.locator("option").allTextContents();
		out.lists = await page.locator(".fqp-list").allInnerTexts();
		out.rows = await page.locator(".fqp-table tbody tr").count();
		out.editable_cells = await page.locator(".fqp-table input.fqp-rate").count();
		out.locked_cells = await page.locator(".fqp-table td.fqp-locked").count();
		out.also_sold_by = await page.locator(".fqp-also").count();
		await page.screenshot({ path: `${shots}/${out.user}-page.png`, fullPage: false });
		if (!out.rows) fail("no rows");
		if (!!out.editable_cells !== u.edits) fail("editable cells");
		if (!out.lists.some((t) => t.includes("a change applies to"))) fail("shared-list notice");

		// 4. a writer adds another selling list from the picker: a new column with its own notice line
		if (u.edits) {
			const picker = page.locator('.page-form select[data-fieldname="add_list"]');
			const extra = (await picker.locator("option").allTextContents()).find((o) => o && !out.lists.some((t) => t.startsWith(o + " ")));
			if (extra) {
				await picker.selectOption(extra);
				await page.waitForLoadState("networkidle");
				await sleep(2500);
				out.added_list = { name: extra, lists: (await page.locator(".fqp-list").allInnerTexts()).length, header: await page.locator(".fqp-table thead").innerText() };
				if (!out.added_list.header.includes(extra)) fail("added list column");
				await page.screenshot({ path: `${shots}/${out.user}-added-list.png` });
				await page.locator(`.fqp-remove-list[data-list="${extra}"]`).click();
				await page.waitForLoadState("networkidle");
				await sleep(2000);
				if ((await page.locator(".fqp-table thead").innerText()).includes(extra)) fail("remove added list");
			}
		}
		// 5. a writer saves one change through the dialog; the audit row appears
		if (u.edits) {
			const input = page.locator(".fqp-table input.fqp-rate").filter({ hasNot: page.locator("xx") });
			let target = null;
			for (let i = 0; i < (await input.count()); i++) {
				const v = await input.nth(i).inputValue();
				if (v) {
					target = input.nth(i);
					break;
				}
			}
			if (!target) fail("no priced editable cell");
			else {
				out.edited = { item: await target.getAttribute("data-item"), list: await target.getAttribute("data-list") };
				const old = parseFloat(await target.inputValue());
				out.edited.old = old;
				out.edited.new = Math.round(old * 1.05);
				await target.fill(String(out.edited.new));
				await target.dispatchEvent("change");
				await page.getByRole("button", { name: /^Save 1 Change$/ }).click();
				await sleep(800);
				out.dialog = await page.evaluate(() => {
					const d = $(".modal:visible");
					return { modals: d.length, title: d.find(".modal-title").text(), buttons: d.find("button").map((i, b) => $(b).text().trim()).get() };
				});
				await page.screenshot({ path: `${shots}/${out.user}-dialog.png` });
				await page.locator(".modal.show").getByRole("button", { name: "Save", exact: true }).click({ timeout: 8000 });
				await page.waitForLoadState("networkidle");
				await sleep(2500);
				await page.screenshot({ path: `${shots}/${out.user}-after-save.png` });
				const log = await ctx.request.get(
					`${BASE}/api/resource/Quick Price Change?fields=${encodeURIComponent('["item_code","price_list","old_rate","new_rate","user","action"]')}&order_by=creation%20desc&limit_page_length=1`
				);
				out.log = (await log.json()).data?.[0];
				if (!out.log || out.log.item_code !== out.edited.item || out.log.new_rate !== out.edited.new) fail("audit row");
			}
		} else {
			// the server refuses a direct call too
			const headers = { "X-Frappe-CSRF-Token": await page.evaluate(() => frappe.csrf_token) };
			const items = await ctx.request.post(`${BASE}/api/method/fours_customizations.quick_price.api.get_items`, {
				form: { company: u.company, page_length: 1 },
				headers,
			});
			const first = ((await items.json()).message || { items: [] }).items[0];
			const cell = first && Object.entries(first.prices).find(([, c]) => c.item_price);
			if (!cell) fail("no priced cell for the direct call");
			if (cell) {
				const r = await ctx.request.post(`${BASE}/api/method/fours_customizations.quick_price.api.save_prices`, {
					headers,
					form: {
						company: u.company,
						changes: JSON.stringify([
							{ item_code: first.item_code, price_list: cell[0], uom: first.uom, item_price: cell[1].item_price, expected_old_rate: cell[1].rate, new_rate: cell[1].rate + 1 },
						]),
					},
				});
				out.direct_save = (await r.json()).message?.[0];
				if (!out.direct_save || out.direct_save.ok !== 0 || out.direct_save.error !== "PermissionError") fail("direct save not refused");
			}
		}
		// 6. Pricing workspace shortcut
		await page.goto(`${BASE}/desk/pricing`, { waitUntil: "networkidle" });
		await sleep(2000);
		out.workspace_shortcut = await page.locator('.shortcut-widget-box:has-text("Quick Price Update"), .widget:has-text("Quick Price Update")').count();
		await page.screenshot({ path: `${shots}/${out.user}-pricing-workspace.png` });
		if (!out.workspace_shortcut) fail("workspace shortcut");
	} else {
		out.page_text = (await page.locator("body").innerText()).slice(0, 200).replace(/\s+/g, " ");
		out.page_rows = await page.locator(".fqp-table tbody tr").count();
		if (out.page_rows) fail("page shows data");
		const r = await ctx.request.post(`${BASE}/api/method/fours_customizations.quick_price.api.get_context`, {
			headers: { "X-Frappe-CSRF-Token": await page.evaluate(() => frappe.csrf_token) },
		});
		out.api_status = r.status();
		if (r.status() !== 403) fail("api not refused");
		await page.screenshot({ path: `${shots}/${out.user}-page.png` });
	}
	} catch (e) {
		fail("exception: " + String(e).split("\n")[0]);
	}
	console.log(JSON.stringify(out));
	await ctx.close();
}
await browser.close();
process.exit(failed ? 1 : 0);
