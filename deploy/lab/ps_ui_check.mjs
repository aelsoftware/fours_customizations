// ps-fours-2 LAB ONLY: headless Chromium check of the selling-list form script on the private lab bench (kasc-bom-tills):
//   node ps_ui_check.mjs <ps-ui.json> <screenshot dir>
// fqpm.lab after ps2 apply (Standard Selling disabled), fixtures from ps_ui_seed.py. Checks, as a desk user:
//   amend   the cancelled pre-split invoice: the list switches to the company's list without an error, the line rates stay;
//           a qty change on a line raises no 'disabled' error; the amendment saves
//   dup     duplicate of a submitted pre-split invoice: list switched, no error
//   stale   a new invoice in a tab whose default is still 'Standard Selling': list switched, no error
// Every request to another host is aborted and counted. Prints one JSON line; exit 1 when an expectation fails.
import fs from "node:fs";
import { chromium } from "/opt/kr-playwright/node_modules/playwright/index.mjs";

const BASE = "http://10.67.210.10:18223";
const [cfgFile, shots] = process.argv.slice(2);
const cfg = JSON.parse(fs.readFileSync(cfgFile, "utf8"));
const LIST = "Vox Nansana Selling Price";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const out = { blocked: 0, console_errors: [], dialogs: [], failures: [] };
const fail = (what) => out.failures.push(what);

const browser = await chromium.launch({ executablePath: "/opt/ms-playwright/chromium-1243/chrome-linux64/chrome" });
const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
await ctx.route("**/*", (route) => {
	if (new URL(route.request().url()).host === "10.67.210.10:18223") return route.continue();
	out.blocked++;
	return route.abort();
});
out.login = (await ctx.request.post(`${BASE}/api/method/login`, { form: { usr: cfg.email, pwd: cfg.password } })).status();
const page = await ctx.newPage();
page.on("console", (m) => m.type() === "error" && out.console_errors.push(m.text().slice(0, 200)));
page.on("pageerror", (e) => out.console_errors.push("pageerror: " + String(e).slice(0, 300)));
out.http_errors = [];
page.on("response", (r) => r.status() >= 400 && out.http_errors.push(`${r.status()} ${r.url().replace(BASE, "")}`.slice(0, 160)));
const dialogs = async (label) => {
	const texts = await page.evaluate(() =>
		[...document.querySelectorAll(".modal.show .modal-body, .msgprint")].map((e) => e.innerText.trim()).filter(Boolean)
	);
	for (const t of texts) out.dialogs.push(`${label}: ${t.slice(0, 200)}`);
	if (texts.some((t) => /disabled|does not exist/i.test(t))) fail(`${label}: disabled-list error shown`);
	await page.evaluate(() => document.querySelectorAll(".modal.show .btn-modal-close").forEach((b) => b.click()));
};
const formState = () =>
	page.evaluate(() => ({
		name: cur_frm.doc.name,
		local: !!cur_frm.doc.__islocal,
		list: cur_frm.doc.selling_price_list,
		amended_from: cur_frm.doc.amended_from || null,
		rates: Object.fromEntries(
			(cur_frm.doc.items || []).map((i) => [String(i.item_code), flt(i.rate || i.price_list_rate)])
		),
		lines: (cur_frm.doc.items || []).map((i) => [i.item_code, i.qty, i.rate, i.price_list_rate, i.amount]),
	}));

try {
	// amend
	await page.goto(`${BASE}/app/sales-invoice/${cfg.invoices.amend}`, { waitUntil: "networkidle" });
	await sleep(2000);
	await page.evaluate(() => cur_frm.amend_doc());
	await sleep(5000);
	out.amend = await formState();
	await dialogs("amend");
	if (!out.amend.local || out.amend.list !== LIST) fail(`amend: list ${out.amend.list}`);
	for (const [code, rate] of Object.entries(cfg.rates))
		if (Math.abs((out.amend.rates[code] || 0) - rate) > 0.001) fail(`amend: ${code} re-priced to ${out.amend.rates[code]}`);
	await page.evaluate(() => frappe.model.set_value(cur_frm.doc.items[0].doctype, cur_frm.doc.items[0].name, "qty", 2));
	await sleep(4000);
	await dialogs("amend qty");
	out.amend_after_qty = await formState();
	await page.screenshot({ path: `${shots}/amend.png` });
	await page.evaluate(() => cur_frm.save());
	await sleep(5000);
	await dialogs("amend save");
	out.amend_saved = await formState();
	if (out.amend_saved.local) fail("amend: not saved");

	// duplicate
	await page.goto(`${BASE}/app/sales-invoice/${cfg.invoices.duplicate}`, { waitUntil: "networkidle" });
	await sleep(2000);
	await page.evaluate(() => cur_frm.copy_doc());
	await sleep(5000);
	out.dup = await formState();
	await dialogs("duplicate");
	if (out.dup.list !== LIST) fail(`duplicate: list ${out.dup.list}`);

	// a tab whose default list is still the archive (the boot defaults of a tab opened before the window)
	// (company from the user's defaults, as a new document gets it from Desk)
	await page.evaluate(() => {
		window.__apl = [];
		const call = frappe.call;
		frappe.call = function (opts) {
			if (opts && String(opts.method || "").includes("apply_price_list"))
				window.__apl.push({
					list: opts.args && opts.args.ctx && opts.args.ctx.price_list,
					at: new Error().stack.split("\n").slice(2, 12).map((l) => l.trim().slice(0, 90)).join(" | "),
				});
			return call.apply(this, arguments);
		};
		frappe.boot.sysdefaults.selling_price_list = "Standard Selling";
		frappe.boot.sysdefaults.company = "Vox Lounge Nansana";
		const d = (frappe.boot.user.defaults = frappe.boot.user.defaults || {});
		d.selling_price_list = "Standard Selling";
		d.company = "Vox Lounge Nansana";
		frappe.new_doc("Sales Invoice");
	});
	await sleep(5000);
	out.stale = await formState();
	out.stale_apply_price_list = await page.evaluate(() => window.__apl);
	await dialogs("stale tab");
	if (out.stale.list !== LIST) fail(`stale tab: list ${out.stale.list}`);
	await page.screenshot({ path: `${shots}/stale.png` });
} catch (e) {
	fail("exception " + String(e).slice(0, 300));
}
if (out.blocked) fail(`${out.blocked} requests to other hosts`);
console.log(JSON.stringify(out));
await browser.close();
process.exit(out.failures.length ? 1 : 0);
