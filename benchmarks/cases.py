"""Task definitions and state-based grading, never supplied to the planner."""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    id: str
    path: str
    task: str
    description: str


CASES = [
    Case("profile", "/profile.html", "建立活動報名：姓名林沛安，Email pei.an@example.test，部門選設計，座位選靠窗，勾選需要茶點，然後送出報名。", "文字輸入、兩個原生下拉選單、核取方塊、送出"),
    Case("inventory", "/inventory.html", "在庫存管理搜尋鍵盤，將機械鍵盤（B-204）的庫存改成 27 並儲存。保留鍵盤搜尋條件，其他商品的庫存不可更動。", "搜尋表格、辨識正確資料列、修改數值、儲存"),
    Case("supplier", "/order.html", "開啟供應商目錄的新分頁，查出星河工作室的配送代碼及服務窗口 Email，填回補貨申請，供應商填星河工作室、數量填 24，並送出申請。保留申請及目錄兩個分頁。", "跨分頁檢索、記憶資料、返回填寫、送出"),
]


def _stored(snapshot: dict, key: str):
    for origin in snapshot.get("storage", {}).get("origins", []):
        for item in origin.get("localStorage", []):
            if item.get("name") == key:
                try:
                    return json.loads(item["value"])
                except (TypeError, ValueError):
                    return None
    return None


def grade(case_id: str, snapshot: dict) -> dict:
    """A model's 'done' statement has no influence on these checks."""
    if case_id == "profile":
        actual = _stored(snapshot, "bench_profile")
        expected = {"name": "林沛安", "email": "pei.an@example.test", "department": "design", "seat": "window", "snack": True, "submitted": True}
        checks = {key: actual is not None and actual.get(key) == value for key, value in expected.items()}
    elif case_id == "inventory":
        actual = _stored(snapshot, "bench_inventory")
        expected = {"A-101": 18, "B-204": 27, "C-310": 5, "D-402": 8}
        stocks = actual.get("stocks", {}) if isinstance(actual, dict) else {}
        checks = {f"stock_{sku}": stocks.get(sku) == quantity for sku, quantity in expected.items()}
        checks["saved_target"] = actual is not None and actual.get("saved") == ["B-204"]
        checks["filter_preserved"] = _stored(snapshot, "bench_inventory_filter") == "鍵盤"
        checks["visible_target_only"] = _stored(snapshot, "bench_inventory_visible") == ["B-204"]
    elif case_id == "supplier":
        actual = _stored(snapshot, "bench_order")
        expected = {"supplier": "星河工作室", "code": "GH-742", "email": "supply@starlake.example.test", "quantity": 24, "submitted": True}
        checks = {key: actual is not None and actual.get(key) == value for key, value in expected.items()}
        paths = {item.get("path") for item in snapshot.get("pages", [])}
        checks["order_tab_open"] = "/order.html" in paths
        checks["directory_tab_open"] = "/directory.html" in paths
    else:
        raise ValueError(f"Unknown benchmark case: {case_id}")
    return {"passed": all(checks.values()), "checks": checks, "observed_state": actual}
