"""Parent-only core verifier adapters for the unchanged benchmark oracles.

These specifications are never planner tool arguments or observation payloads.
The planner receives Case.task and ordinary driver observations. In particular,
the supplier answer must still be found in the original directory page.

DOM-only conditions intentionally remain empty: the original grade() checks
committed localStorage records and open tabs. Requiring current form values or
accepting a status banner would change the benchmark's success definition.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from benchmarks.cases import CASES, Case, grade
from server.core.contracts import SuccessCriterion
from server.core.verifiers import BrowserCondition, CheckResult
from server.drivers import BrowserDriver


# Human-readable criterion descriptions contain no hidden expected values or
# selectors. Expected values remain solely in the original parent-side oracle.
_DESCRIPTIONS = {
    "profile": {
        "name": "已提交的報名姓名符合原任務",
        "email": "已提交的報名 Email 符合原任務",
        "department": "已提交的部門符合原任務",
        "seat": "已提交的座位偏好符合原任務",
        "snack": "已提交的茶點選擇符合原任務",
        "submitted": "報名確實已提交",
    },
    "inventory": {
        "stock_A-101": "第一項非目標商品的已儲存庫存未改動",
        "stock_B-204": "目標商品的已儲存庫存符合原任務",
        "stock_C-310": "第二項非目標商品的已儲存庫存未改動",
        "stock_D-402": "第三項非目標商品的已儲存庫存未改動",
        "saved_target": "儲存紀錄只包含原任務指定商品",
        "filter_preserved": "保留原任務指定的搜尋條件",
        "visible_target_only": "搜尋後的可見商品紀錄符合原任務",
    },
    "supplier": {
        "supplier": "已提交的供應商符合原任務",
        "code": "已提交的配送代碼符合原始目錄資料",
        "email": "已提交的服務窗口 Email 符合原始目錄資料",
        "quantity": "已提交的數量符合原任務",
        "submitted": "補貨申請確實已提交",
        "order_tab_open": "保留補貨申請分頁",
        "directory_tab_open": "保留供應商目錄分頁",
    },
}


async def _capture_snapshot(driver: BrowserDriver) -> dict:
    """Same independent readback as the original benchmark's close wrapper."""
    context = driver._context
    if context is None:
        raise RuntimeError("Benchmark browser is unavailable")
    storage = await context.storage_state()
    pages = [{"url": page.url, "path": urlsplit(page.url).path}
             for page in context.pages if not page.is_closed()]
    return {"storage": storage, "pages": pages}


@dataclass(frozen=True)
class BenchmarkCoreSpec:
    """Trusted-parent registration data, not a serializable planner response."""

    case: Case
    verifier_id: str
    success_criteria: tuple[SuccessCriterion, ...]
    check_keys: tuple[str, ...]
    browser_conditions: tuple[BrowserCondition, ...] = ()

    def make_callback(self) -> Callable[[BrowserDriver], Awaitable[tuple[CheckResult, ...]]]:
        case_id, criteria, keys = self.case.id, self.success_criteria, self.check_keys

        async def verify(driver: BrowserDriver) -> tuple[CheckResult, ...]:
            try:
                result = grade(case_id, await _capture_snapshot(driver))
                checks = result["checks"]
                if tuple(checks) != keys or any(type(value) is not bool for value in checks.values()):
                    raise ValueError("Original oracle contract changed")
                if result["passed"] is not all(checks.values()):
                    raise ValueError("Original oracle result is inconsistent")
                statuses = tuple("pass" if checks[key] else "fail" for key in keys)
            except Exception:
                # A missing/closed context, failed capture or malformed stored
                # data cannot become success. Do not return raw exceptions or
                # stored answers. CancelledError intentionally propagates.
                statuses = ("inconclusive",) * len(criteria)
            return tuple(CheckResult(criterion_id=criterion.id, status=status,
                reason="oracle_" + status)
                for criterion, status in zip(criteria, statuses, strict=True))

        return verify


def core_spec(case: Case | str) -> BenchmarkCoreSpec:
    """Map one original case to criteria plus a parent-only callback maker.

    Register with registry.register_callback(task, spec.make_callback(),
    verifier_id=spec.verifier_id). Task.goal must remain spec.case.task. Never
    include this spec, callback readback or grade() output in a planner prompt.

    This preserves the original oracle's limits: inventory's saved-SKU list
    does not count repeated saves, and supplier state/tabs do not prove the
    historical retrieval process. Neither is silently strengthened here.
    """
    case_id = case.id if isinstance(case, Case) else case
    canonical = next((item for item in CASES if item.id == case_id), None)
    if canonical is None:
        raise ValueError("Unknown benchmark case")
    if isinstance(case, Case) and case != canonical:
        raise ValueError("Core benchmark must use the unchanged original case")
    keys = tuple(grade(canonical.id, {})["checks"])
    descriptions = _DESCRIPTIONS[canonical.id]
    if set(keys) != set(descriptions):
        raise ValueError("Original benchmark checks require an explicit specification update")
    verifier_id = f"benchmark-{canonical.id}-oracle-v1"
    criteria = tuple(SuccessCriterion(id=f"benchmark-{canonical.id}-{key}",
        description=descriptions[key], verifier_ref=verifier_id) for key in keys)
    return BenchmarkCoreSpec(canonical, verifier_id, criteria, keys)
