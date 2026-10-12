"""Read-only kitchen capacity and correlated native reservations; no live site."""

from copy import deepcopy
from decimal import Decimal
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest import TestCase
from unittest.mock import patch


class TestVoxCatalogueStock(TestCase):
    def setUp(self):
        self.frappe = ModuleType("frappe")
        self.frappe.conf = {"vox_goat_work_orders": {"Vox Lounge Nansana": True}}
        self.frappe.local = SimpleNamespace(site="multax.kit.africa")
        self.frappe.db = SimpleNamespace(sql=self.sql, get_value=self.get_value)
        self.enterContext(patch.dict(sys.modules, {"frappe": self.frappe}))
        path = Path(__file__).resolve().parents[1] / "vox_catalogue_stock.py"
        spec = importlib.util.spec_from_file_location("fours_customizations._isolated_catalogue_stock", path)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.queries, self.work_orders, self.reservations = [], [], []
        self.reads = []
        self.goat_menu = frozenset(("VXN-DRY-GOAT", "VXN-PAN-FRIED-GOAT"))
        self.warehouse = {"company": self.mod.COMPANY, "is_group": 0, "disabled": 0}
        self.location = {"company": self.mod.COMPANY, "name": "Main Bar"}
        self.items = {
            code: {
                "stock_uom": self.mod.RAW_UOMS.get(code, "Pc(s)"),
                "is_stock_item": 1, "disabled": 0, "custom_company": self.mod.COMPANY,
            }
            for code in (*self.mod.RAW_UOMS, *self.mod.MENU_ITEMS)
        }
        self.uoms = {uom: {"enabled": 1, "must_be_whole_number": 0} for uom in self.mod.RAW_UOMS.values()}
        self.bins = {self.mod.RAW_ITEM: self.bin(actual_qty=2)}

    def bin(self, **kwargs):
        row = dict(
            name="native-bin", actual_qty=0, reserved_qty=0,
            reserved_qty_for_production=0, reserved_qty_for_sub_contract=0,
            reserved_qty_for_production_plan=0, reserved_stock=0,
        )
        row.update(kwargs)
        return row

    def sql(self, query, params, *, as_dict):
        assert as_dict is True
        assert query.lstrip().startswith("SELECT")
        self.queries.append((query, params))
        item_code, warehouse = params
        if warehouse != self.mod.WAREHOUSE:
            return []
        if "FROM `tabBin`" in query:
            return [deepcopy(self.bins[item_code])] if item_code in self.bins else []
        if "FROM `tabWork Order`" in query:
            assert "wo.docstatus = 1" in query
            assert "wo.status NOT IN ('Stopped', 'Completed', 'Closed')" in query
            assert "wi.item_code = %s AND wi.source_warehouse = %s" in query
            assert "wi.parenttype = 'Work Order'" in query
            return [deepcopy(row) for row in self.work_orders if (
                row.get("item_code", self.mod.RAW_ITEM) == item_code
                and row.get("docstatus", 1) == 1
                and row.get("status", "Not Started") not in ("Stopped", "Completed", "Closed")
                and (row["required_qty"] > row["transferred_qty"] or row["required_qty"] > row["consumed_qty"])
            )]
        if "FROM `tabStock Reservation Entry`" in query:
            assert "docstatus = 1 AND item_code = %s AND warehouse = %s" in query
            assert "delivered_qty < reserved_qty" in query
            return [deepcopy(row) for row in self.reservations if (
                row.get("item_code", self.mod.RAW_ITEM) == item_code
                and row.get("docstatus", 1) == 1
                and (isinstance(row["reserved_qty"], str) or row["delivered_qty"] < row["reserved_qty"])
            )]
        raise AssertionError("Unexpected SQL")

    def get_value(self, doctype, name, fields, *, as_dict):
        assert as_dict
        self.reads.append((doctype, name, fields))
        if doctype == "Warehouse":
            assert name == self.mod.WAREHOUSE
            return deepcopy(self.warehouse)
        if doctype == "Item":
            return deepcopy(self.items.get(name))
        if doctype == "UOM":
            return deepcopy(self.uoms.get(name))
        raise AssertionError("Unexpected document read")

    def wo(self, name="WO-1", detail="ROW-1", **values):
        row = dict(work_order=name, detail=detail, skip_transfer=1,
                   required_qty=Decimal("0.4"), transferred_qty=0, consumed_qty=0)
        row.update(values)
        self.work_orders.append(row)
        self.recalculate()
        return row

    def sre(self, name="SRE-1", **values):
        row = dict(name=name, voucher_type="Work Order", voucher_no="WO-1", voucher_detail_no="ROW-1",
                   reserved_qty=Decimal("0.4"), delivered_qty=0, transferred_qty=0, consumed_qty=0)
        row.update(values)
        self.reservations.append(row)
        self.recalculate()
        return row

    def recalculate(self):
        for code in self.mod.RAW_UOMS:
            raw = self.bins.get(code)
            if raw is None:
                continue
            raw["reserved_qty_for_production"] = sum(
                max(0, row["required_qty"] - row["consumed_qty" if row["skip_transfer"] else "transferred_qty"])
                for row in self.work_orders if row.get("docstatus", 1) == 1
                and row.get("status") not in ("Stopped", "Completed", "Closed")
                and row.get("item_code", self.mod.RAW_ITEM) == code
            )
            raw["reserved_stock"] = sum(
                row["reserved_qty"] - row["delivered_qty"] - row["transferred_qty"] - row["consumed_qty"]
                for row in self.reservations if row.get("docstatus", 1) == 1
                and row["delivered_qty"] < row["reserved_qty"]
                and row.get("item_code", self.mod.RAW_ITEM) == code
            )

    def project(self, **kwargs):
        values = dict(company=self.mod.COMPANY, location=self.location, item_codes=sorted(self.goat_menu))
        values.update(kwargs)
        return self.mod.catalogue_availability(**values)

    def held(self, **kwargs):
        return self.mod.reservation_hold(self.mod.RAW_ITEM, self.mod.WAREHOUSE, **kwargs)

    def test_offdefault_and_unrelated_company_have_no_reads(self):
        self.frappe.conf.clear()
        assert self.project() == {}
        for config in ({}, {self.mod.COMPANY: False}, {self.mod.COMPANY: 0}):
            self.frappe.conf[self.mod.CONFIG] = config
            assert self.project() == {}
        self.frappe.conf[self.mod.CONFIG] = "malformed PRIVATE"
        assert self.project(company="Vox Lounge") == {}
        self.frappe.local.site = "unrelated.kit.africa"
        assert self.project() == {}
        assert not self.queries

    def test_selected_configuration_is_strict(self):
        for value in (None, "1", [], {}, 2, 0.5):
            self.frappe.conf[self.mod.CONFIG] = {self.mod.COMPANY: value}
            with self.assertRaises(ValueError):
                self.project()
        for value in (None, []):
            self.frappe.conf[self.mod.CONFIG] = value
            with self.assertRaises(ValueError):
                self.project()

    def test_two_preparations_share_one_five_dish_pool(self):
        result = self.project()
        assert result == {code: {"qty_available": 5.0} for code in self.goat_menu}
        self.wo()
        # One new native reservation reduces BOTH choices, not just one SKU.
        assert self.project() == {code: {"qty_available": 4.0} for code in self.goat_menu}
        assert self.held() == Decimal("0.4")

    def test_finished_goods_are_origin_bound_and_block_only_the_same_new_sku(self):
        self.bins["VXN-DRY-GOAT"] = self.bin(actual_qty=1)
        for finished in (1, -1, Decimal("0.000001")):
            self.bins["VXN-DRY-GOAT"]["actual_qty"] = finished
            result = self.project()
            assert result["VXN-DRY-GOAT"]["qty_available"] == 0
            assert result["VXN-PAN-FRIED-GOAT"]["qty_available"] == 5
        self.bins["VXN-PAN-FRIED-GOAT"] = self.bin(actual_qty=1)
        assert self.project() == {code: {"qty_available": 0.0} for code in self.goat_menu}
        self.bins[self.mod.RAW_ITEM]["actual_qty"] = 0
        assert self.project() == {code: {"qty_available": 0.0} for code in self.goat_menu}

    def test_shortage_fraction_floor_missing_bins_and_negative_inventory(self):
        for raw, expected in (("0.39", 0), ("0.4", 1), ("1.999999", 4), ("-1", 0), ("0", 0)):
            self.bins[self.mod.RAW_ITEM]["actual_qty"] = Decimal(raw)
            assert {value["qty_available"] for value in self.project().values()} == {expected}
        self.bins[self.mod.RAW_ITEM]["actual_qty"] = 2
        self.bins.clear()
        assert {value["qty_available"] for value in self.project().values()} == {0}

    def test_same_work_order_sre_is_counted_once_and_own_hold_can_be_excluded(self):
        self.wo()
        self.sre()
        assert self.held() == Decimal("0.4")
        assert self.held(exclude_work_order="WO-1") == 0
        assert {value["qty_available"] for value in self.project().values()} == {4}

    def test_independent_reservations_are_added_not_global_max(self):
        self.wo()
        self.sre()
        self.sre("SRE-2", voucher_type="Sales Order", voucher_no="SO-1", voucher_detail_no="SO-ROW")
        self.wo("WO-2", "ROW-2", required_qty=Decimal("0.8"))
        assert self.held() == Decimal("1.6")
        assert self.held(exclude_work_order="WO-1") == Decimal("1.2")
        assert {value["qty_available"] for value in self.project().values()} == {1}

    def test_multiple_sres_for_same_detail_are_summed_before_deduplication(self):
        self.wo()
        self.sre(reserved_qty=Decimal("0.3"))
        self.sre("SRE-2", reserved_qty=Decimal("0.3"))
        assert self.held() == Decimal("0.6")
        self.wo("WO-1", "ROW-2")
        assert self.held() == Decimal("1.0")
        assert self.held(exclude_work_order="WO-1") == 0

    def test_partial_consumption_and_transfer_match_native_remaining(self):
        self.wo(consumed_qty=Decimal("0.1"))
        self.sre(consumed_qty=Decimal("0.1"))
        assert self.held() == Decimal("0.3")
        self.wo("WO-2", "ROW-2", skip_transfer=0, transferred_qty=Decimal("0.2"))
        self.sre("SRE-2", voucher_no="WO-2", voucher_detail_no="ROW-2", transferred_qty=Decimal("0.2"))
        assert self.held() == Decimal("0.5")
        self.sre("SRE-3", voucher_type="Sales Order", delivered_qty=Decimal("0.2"))
        assert self.held() == Decimal("0.7")

    def test_cancelled_closed_and_fully_used_records_do_not_hold_capacity(self):
        self.wo(status="Completed")
        self.wo("WO-2", "ROW-2", docstatus=2)
        self.wo("WO-3", "ROW-3", status="Stopped")
        self.wo("WO-4", "ROW-4", consumed_qty=Decimal("0.4"))
        self.sre(docstatus=2)
        self.sre("SRE-2", consumed_qty=Decimal("0.4"))
        self.sre("SRE-3", transferred_qty=Decimal("0.4"))
        self.sre("SRE-4", delivered_qty=Decimal("0.4"))
        assert self.held() == 0
        assert {value["qty_available"] for value in self.project().values()} == {5}

    def test_unmatched_work_order_sre_still_holds_stock(self):
        self.sre(voucher_no="UNMATCHED-WO", voucher_detail_no="UNMATCHED-ROW")
        assert self.held() == Decimal("0.4")

    def test_unexplained_forecasts_and_bin_totals_fail_closed(self):
        raw = self.bins[self.mod.RAW_ITEM]
        for field in (*self.mod._OTHER_FORECASTS, "reserved_qty_for_production", "reserved_stock"):
            raw[field] = Decimal("0.4")
            with self.assertRaises(ValueError):
                self.project()
            raw[field] = 0
        self.wo()
        raw["reserved_qty_for_production"] = 0
        with self.assertRaises(ValueError):
            self.held(exclude_work_order="WO-1")

    def test_malformed_reservations_fail_closed(self):
        for field, value in (("reserved_qty", "NaN"), ("consumed_qty", -1), ("voucher_detail_no", "")):
            self.reservations.clear()
            row = self.sre()
            row[field] = value
            with self.assertRaises(ValueError):
                self.held()
        self.reservations.clear()
        self.sre()
        self.reservations[0]["consumed_qty"] = 1
        with self.assertRaises(ValueError):
            self.held()

    def test_only_authorized_goat_codes_are_projected_in_pieces(self):
        assert self.project(item_codes=["OTHER", self.mod.RAW_ITEM]) == {}
        one = self.project(item_codes=["VXN-DRY-GOAT", self.mod.RAW_ITEM, "OTHER"])
        assert one == {"VXN-DRY-GOAT": {"qty_available": 5.0}}
        self.items["VXN-DRY-GOAT"]["stock_uom"] = "Kg"
        with self.assertRaises(ValueError):
            self.project()

    def test_wrong_company_warehouse_disabled_and_nonstock_dishes_fail_closed(self):
        with self.assertRaises(ValueError):
            self.project(location={"company": "Vox Lounge"})
        for field, value in (("company", "Vox Lounge"), ("is_group", 1), ("disabled", 1)):
            before = deepcopy(self.warehouse)
            self.warehouse[field] = value
            with self.assertRaises(ValueError):
                self.project()
            self.warehouse = before
        for field, value in (("disabled", 1), ("is_stock_item", 0)):
            self.items["VXN-DRY-GOAT"][field] = value
            with self.assertRaises(ValueError):
                self.project()
            self.items["VXN-DRY-GOAT"][field] = 0 if field == "disabled" else 1

    def test_public_result_has_no_ingredients_weights_costs_or_stock_writes(self):
        before = deepcopy((self.items, self.bins, self.work_orders, self.reservations, self.frappe.conf))
        output = self.project()
        assert self.mod.RAW_ITEM not in str(output)
        assert "Kg" not in str(output) and "cost" not in str(output)
        assert all(set(row) == {"qty_available"} for row in output.values())
        assert before == (self.items, self.bins, self.work_orders, self.reservations, self.frappe.conf)
        assert not any("FOR UPDATE" in query for query, params in self.queries)

    def test_manufacturing_can_request_current_locking_reads(self):
        self.wo()
        assert self.held(for_update=True, exclude_work_order="WO-1") == 0
        assert len(self.queries) == 3
        assert all(query.endswith(" FOR UPDATE") for query, params in self.queries)

    def kitchen_stock(self, *, chicken="1", irish="0.3"):
        self.bins["VXN-RAW-CHICKEN-BIRD"] = self.bin(actual_qty=Decimal(chicken))
        self.bins["VXN-RAW-IRISH-KG"] = self.bin(actual_qty=Decimal(irish))

    def all_dishes(self, **kwargs):
        return {
            code: row["qty_available"]
            for code, row in self.project(item_codes=sorted(self.mod.MENU_ITEMS), **kwargs).items()
        }

    def test_five_dishes_draw_independently_from_shared_ingredient_pools(self):
        self.kitchen_stock()
        assert self.all_dishes() == {
            "VXN-DRY-GOAT": 5.0, "VXN-PAN-FRIED-GOAT": 5.0,
            "VXN-CHICKEN": 4.0, "VXN-CHICKEN-BOWL": 3.0, "VXN-GOAT-BOWL": 3.0,
        }
        # This is a per-choice capacity, not twelve independently reserved
        # chicken/bowl portions or a sum of capacities on a shared raw pool.
        before = deepcopy(self.bins)
        assert self.all_dishes()["VXN-CHICKEN-BOWL"] == 3
        assert self.bins == before

    def test_one_bird_gives_four_plates_without_rounding_up(self):
        for birds, expected in (
            ("0", 0), ("0.249999", 0), ("0.25", 1), ("0.999999", 3),
            ("1", 4), ("2", 8), ("-1", 0),
        ):
            with self.subTest(birds=birds):
                self.kitchen_stock(chicken=birds)
                assert self.project(item_codes=["VXN-CHICKEN"]) == {
                    "VXN-CHICKEN": {"qty_available": float(expected)},
                }

    def test_bowls_require_exactly_one_hundred_grams_of_irish_per_plate(self):
        for irish, expected in (
            ("0", 0), ("0.099999", 0), ("0.1", 1), ("0.199999", 1),
            ("0.2", 2), ("0.4", 4), ("-0.1", 0),
        ):
            with self.subTest(irish=irish):
                self.kitchen_stock(irish=irish)
                values = self.all_dishes()
                assert values["VXN-CHICKEN-BOWL"] == expected
                assert values["VXN-GOAT-BOWL"] == expected
                assert values["VXN-CHICKEN"] == 4
                assert values["VXN-PAN-FRIED-GOAT"] == 5

    def test_each_bowl_is_limited_by_its_meat_even_with_ample_irish(self):
        self.kitchen_stock(chicken="0.5", irish="100")
        self.bins[self.mod.RAW_ITEM]["actual_qty"] = Decimal("0.4")
        values = self.all_dishes()
        assert values["VXN-CHICKEN-BOWL"] == 2
        assert values["VXN-GOAT-BOWL"] == 1
        self.bins[self.mod.RAW_ITEM]["actual_qty"] = 0
        assert self.all_dishes()["VXN-GOAT-BOWL"] == 0
        assert self.all_dishes()["VXN-CHICKEN-BOWL"] == 2

    def test_missing_new_raw_bins_do_not_invent_opening_inventory(self):
        values = self.all_dishes()
        assert values["VXN-CHICKEN"] == values["VXN-CHICKEN-BOWL"] == values["VXN-GOAT-BOWL"] == 0
        assert values["VXN-DRY-GOAT"] == values["VXN-PAN-FRIED-GOAT"] == 5
        self.kitchen_stock()
        del self.bins["VXN-RAW-CHICKEN-BIRD"]
        values = self.all_dishes()
        assert values["VXN-CHICKEN"] == values["VXN-CHICKEN-BOWL"] == 0
        assert values["VXN-GOAT-BOWL"] == 3

    def test_chicken_reservation_reduces_plain_and_bowl_capacity_once(self):
        self.kitchen_stock(irish="10")
        self.wo(item_code="VXN-RAW-CHICKEN-BIRD", required_qty=Decimal("0.25"))
        self.sre(item_code="VXN-RAW-CHICKEN-BIRD", reserved_qty=Decimal("0.25"))
        values = self.all_dishes()
        assert values["VXN-CHICKEN"] == values["VXN-CHICKEN-BOWL"] == 3
        assert values["VXN-GOAT-BOWL"] == values["VXN-PAN-FRIED-GOAT"] == 5
        assert self.mod.reservation_hold("VXN-RAW-CHICKEN-BIRD", self.mod.WAREHOUSE) == Decimal("0.25")

    def test_irish_reservation_is_shared_by_bowls_but_not_plain_dishes(self):
        self.kitchen_stock(irish="0.4")
        self.wo(item_code="VXN-RAW-IRISH-KG", required_qty=Decimal("0.1"))
        self.sre(item_code="VXN-RAW-IRISH-KG", reserved_qty=Decimal("0.1"))
        values = self.all_dishes()
        assert values["VXN-CHICKEN-BOWL"] == values["VXN-GOAT-BOWL"] == 3
        assert values["VXN-CHICKEN"] == 4
        assert values["VXN-DRY-GOAT"] == values["VXN-PAN-FRIED-GOAT"] == 5

    def test_one_bowl_work_order_holds_its_two_native_component_rows(self):
        self.kitchen_stock(irish="0.4")
        for code, detail, qty in (
            ("VXN-RAW-CHICKEN-BIRD", "BIRD-ROW", Decimal("0.25")),
            ("VXN-RAW-IRISH-KG", "IRISH-ROW", Decimal("0.1")),
        ):
            self.wo(detail=detail, item_code=code, required_qty=qty)
            self.sre(name="SRE-" + detail, item_code=code, voucher_detail_no=detail, reserved_qty=qty)
        values = self.all_dishes()
        assert values["VXN-CHICKEN"] == values["VXN-CHICKEN-BOWL"] == values["VXN-GOAT-BOWL"] == 3
        for code in ("VXN-RAW-CHICKEN-BIRD", "VXN-RAW-IRISH-KG"):
            assert self.mod.reservation_hold(code, self.mod.WAREHOUSE, exclude_work_order="WO-1") == 0

    def test_independent_irish_reservations_are_added_to_native_wo_hold(self):
        self.kitchen_stock(irish="0.4")
        self.wo(item_code="VXN-RAW-IRISH-KG", required_qty=Decimal("0.1"))
        self.sre(item_code="VXN-RAW-IRISH-KG", reserved_qty=Decimal("0.1"))
        self.sre(
            "OTHER-SRE", item_code="VXN-RAW-IRISH-KG", reserved_qty=Decimal("0.2"),
            voucher_type="Sales Order", voucher_no="SO-IRISH", voucher_detail_no="SO-IRISH-ROW",
        )
        assert self.mod.reservation_hold("VXN-RAW-IRISH-KG", self.mod.WAREHOUSE) == Decimal("0.3")
        values = self.all_dishes()
        assert values["VXN-CHICKEN-BOWL"] == values["VXN-GOAT-BOWL"] == 1

    def test_new_ingredient_reservation_mismatch_or_forecast_fails_closed(self):
        self.kitchen_stock()
        for code in ("VXN-RAW-CHICKEN-BIRD", "VXN-RAW-IRISH-KG"):
            for field in (*self.mod._OTHER_FORECASTS, "reserved_qty_for_production", "reserved_stock"):
                with self.subTest(code=code, field=field):
                    self.bins[code][field] = Decimal("0.1")
                    with self.assertRaises(ValueError):
                        self.all_dishes()
                    self.bins[code][field] = 0

    def test_new_dish_finished_stock_is_origin_bound_not_an_alternative_pool(self):
        self.kitchen_stock(irish="1")
        for code in ("VXN-CHICKEN", "VXN-CHICKEN-BOWL", "VXN-GOAT-BOWL"):
            for quantity in (1, -1, Decimal("0.000001")):
                with self.subTest(code=code, quantity=quantity):
                    self.bins[code] = self.bin(actual_qty=quantity)
                    assert self.all_dishes()[code] == 0
                    del self.bins[code]

    def test_only_selected_recipe_ingredients_are_read(self):
        self.kitchen_stock()
        for code in (self.mod.RAW_ITEM, "VXN-RAW-IRISH-KG"):
            del self.items[code]
        assert self.project(item_codes=["VXN-CHICKEN"]) == {"VXN-CHICKEN": {"qty_available": 4.0}}
        assert {name for doctype, name, fields in self.reads if doctype == "Item"} == {
            "VXN-CHICKEN", "VXN-RAW-CHICKEN-BIRD",
        }
        assert {name for doctype, name, fields in self.reads if doctype == "UOM"} == {"Bird"}

    def test_legacy_goat_projection_does_not_require_new_ingredients(self):
        for code in ("VXN-RAW-CHICKEN-BIRD", "VXN-RAW-IRISH-KG"):
            del self.items[code]
        del self.uoms["Bird"]
        assert self.project() == {code: {"qty_available": 5.0} for code in self.goat_menu}

    def test_new_recipe_ingredient_units_and_company_are_strict(self):
        self.kitchen_stock()
        for code in ("VXN-RAW-CHICKEN-BIRD", "VXN-RAW-IRISH-KG", "VXN-CHICKEN-BOWL"):
            for field, value in (
                ("stock_uom", "Nos"), ("is_stock_item", 0), ("disabled", 1),
                ("custom_company", "Vox Lounge"),
            ):
                with self.subTest(code=code, field=field):
                    original = self.items[code][field]
                    self.items[code][field] = value
                    with self.assertRaises(ValueError):
                        self.all_dishes()
                    self.items[code][field] = original
        self.items[self.mod.RAW_ITEM]["custom_company"] = None
        assert self.all_dishes()["VXN-DRY-GOAT"] == 5

    def test_missing_native_raw_master_fails_closed_instead_of_fabricating_stock(self):
        for code in ("VXN-RAW-CHICKEN-BIRD", "VXN-RAW-IRISH-KG"):
            before = self.items.pop(code)
            with self.assertRaises(ValueError):
                self.all_dishes()
            self.items[code] = before

    def test_raw_uom_must_be_enabled_and_fractional(self):
        for uom in ("Bird", "Kg"):
            for field, value in (("enabled", 0), ("must_be_whole_number", 1), ("enabled", None)):
                with self.subTest(uom=uom, field=field):
                    original = self.uoms[uom][field]
                    self.uoms[uom][field] = value
                    with self.assertRaises(ValueError):
                        self.all_dishes()
                    self.uoms[uom][field] = original
            before = self.uoms.pop(uom)
            with self.assertRaises(ValueError):
                self.all_dishes()
            self.uoms[uom] = before

    def test_all_new_codes_are_read_only_hidden_outside_selected_site_company(self):
        assert self.all_dishes(company="Vox Lounge") == {}
        self.frappe.local.site = "ddamu.kit.africa"
        assert self.all_dishes() == {}
        assert not self.queries and not self.reads

    def test_multi_ingredient_projection_exposes_only_dish_counts(self):
        self.kitchen_stock()
        before = deepcopy((self.items, self.bins, self.work_orders, self.reservations, self.uoms))
        output = self.project(item_codes=sorted(self.mod.MENU_ITEMS))
        assert set(output) == self.mod.MENU_ITEMS
        assert all(set(row) == {"qty_available"} for row in output.values())
        for raw_code in self.mod.RAW_UOMS:
            assert raw_code not in str(output)
        assert "Bird" not in str(output) and "Kg" not in str(output) and "cost" not in str(output)
        assert before == (self.items, self.bins, self.work_orders, self.reservations, self.uoms)
        assert not any("FOR UPDATE" in query for query, params in self.queries)
