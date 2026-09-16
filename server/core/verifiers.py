"""Parent-owned deterministic readback candidate; no model or store mutations.

DOM conditions read the current main document in one fixed JavaScript call.
Visible means CSS-rendered with nonempty geometry, not viewport/occlusion proof.
Callbacks are trusted, compiled parent code, not a Python sandbox. Neither specs
nor expected values belong in planner observations. A result is point-in-time
readback, not a persistence guarantee or a task-completion state transition.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import inspect
import re
import threading
from typing import Annotated, Awaitable, Callable, Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StrictStr, model_validator

from server.drivers import BrowserDriver
from .contracts import Identifier, SuccessCriterion, Task, utc_now

Status = Literal["pass", "fail", "inconclusive"]
Reason = Literal["matched", "mismatch", "missing", "ambiguous", "sensitive",
    "unsupported_element", "invalid_selector", "conditions_passed", "condition_failed",
    "condition_inconclusive", "oracle_pass", "oracle_fail", "oracle_inconclusive",
    "invalid_callback", "unsupported_driver", "no_page", "busy_driver", "page_changed",
    "read_failed", "timeout", "outside_scope"]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always",
                              hide_input_in_errors=True, validate_default=True)


# Deliberately small CSS subset: tag/id/class, selected exact attributes, and
# child/descendant combinators. No engines, XPath, scripts, pseudo selectors,
# selector lists, escapes or frame/shadow traversal. Actual uniqueness is checked
# again in Chromium, never inferred from selector syntax.
_IDENT = r"[A-Za-z_][A-Za-z0-9_-]*"
_ATTRIBUTE = r'''\[(?:id|class|name|role|type|data-testid|data-test|aria-label)(?:=(?:"[^"\\\x00-\x1f]{0,128}"|'[^'\\\x00-\x1f]{0,128}'))?\]'''
_MODIFIER = rf"(?:[.#]{_IDENT}|{_ATTRIBUTE})"
_COMPOUND = rf"(?:{_IDENT}{_MODIFIER}*|{_MODIFIER}+)"
_SELECTOR = re.compile(rf"{_COMPOUND}(?:(?:\s*>\s*|\s+){_COMPOUND}){{0,7}}")


class BrowserCondition(_Frozen):
    id: Identifier
    criterion_id: Identifier
    kind: Literal["text", "value", "checked", "selected_values", "visible", "exists",
                  "url_exact", "url_same_origin"]
    selector: Annotated[StrictStr, Field(min_length=1, max_length=512)] | None = Field(default=None, repr=False)
    value: StrictStr | StrictBool | tuple[StrictStr, ...] = Field(repr=False)

    @model_validator(mode="after")
    def validate_condition(self):
        if self.kind.startswith("url_"):
            if self.selector is not None or not isinstance(self.value, str):
                raise ValueError("URL conditions require a string and no selector")
            BrowserDriver._http_origin(self.value)
            if self.kind == "url_same_origin":
                parsed = urlsplit(self.value)
                if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                    raise ValueError("Same-origin conditions require one exact HTTP(S) origin")
        else:
            if self.selector is None or not _SELECTOR.fullmatch(self.selector):
                raise ValueError("Selector is outside the supported bounded CSS subset")
            if self.kind in {"checked", "visible", "exists"}:
                if not isinstance(self.value, bool):
                    raise ValueError("State conditions require an explicit boolean")
            elif self.kind == "selected_values":
                if not isinstance(self.value, tuple) or len(self.value) > 80:
                    raise ValueError("Selected values require a bounded tuple of strings")
            elif not isinstance(self.value, str):
                raise ValueError("Text/value conditions require an exact string")
        values = self.value if isinstance(self.value, tuple) else (self.value,)
        if any(isinstance(value, str) and len(value) > 8000 for value in values):
            raise ValueError("Expected values exceed the supported size")
        return self


class ConditionResult(_Frozen):
    condition_id: Identifier
    status: Status
    reason: Reason


class CheckResult(_Frozen):
    criterion_id: Identifier
    status: Status
    reason: Reason
    observed_at: AwareDatetime = Field(default_factory=utc_now)
    condition_results: tuple[ConditionResult, ...] = ()


class BrowserVerifierSpec(_Frozen):
    task_id: Identifier
    verifier_id: Identifier
    mode: Literal["dom", "callback"]
    criteria: Annotated[tuple[SuccessCriterion, ...], Field(min_length=1, max_length=200)]
    conditions: Annotated[tuple[BrowserCondition, ...], Field(max_length=200)] = Field(default=(), repr=False)

    @model_validator(mode="after")
    def validate_coverage(self):
        criteria = {criterion.id for criterion in self.criteria}
        if len(criteria) != len(self.criteria):
            raise ValueError("Criterion IDs must be unique")
        if any(criterion.verifier_ref not in (None, self.verifier_id) for criterion in self.criteria):
            raise ValueError("Verifier does not match the criterion's required verifier")
        if len({condition.id for condition in self.conditions}) != len(self.conditions):
            raise ValueError("Condition IDs must be unique")
        if self.mode == "dom" and {condition.criterion_id for condition in self.conditions} != criteria:
            raise ValueError("Every task criterion requires conditions; unknown criteria are forbidden")
        if self.mode == "callback" and self.conditions:
            raise ValueError("Callback specs do not accept DOM condition substitutes")
        return self


ReadbackCallback = Callable[[BrowserDriver], Awaitable[tuple[CheckResult, ...]]]


@dataclass(frozen=True)
class _Registered:
    serialized: str
    callback: ReadbackCallback | None = None


_READ_CONDITIONS = r"""(doc, conditions) => {
  if (doc !== document) throw new Error('Document changed');
  const sensitiveSelector = 'input[type="password"],[data-secret],[data-sensitive],[autocomplete="current-password"],[autocomplete="new-password"],[autocomplete="one-time-code"]';
  const sensitive = el => {
    for(let node=el; node; node=node.parentElement) {
      if(node.matches(sensitiveSelector)) return true;
    }
    return !!el.querySelector(sensitiveSelector);
  };
  const visible = el => {
    if(!el.isConnected || !el.getClientRects().length) return false;
    for(let node=el;node;node=node.parentElement) {
      const style=getComputedStyle(node);
      if(style.display==='none' || ['hidden','collapse'].includes(style.visibility) ||
         Number(style.opacity)===0 || style.contentVisibility==='hidden') return false;
    }
    const box=el.getBoundingClientRect();
    return box.width>0 && box.height>0;
  };
  return conditions.map(condition => {
    const out=(status,reason)=>({condition_id:condition.id,status,reason});
    let matches;
    try { matches=doc.querySelectorAll(condition.selector); }
    catch { return out('inconclusive','invalid_selector'); }
    if(matches.length>1) return out('inconclusive','ambiguous');
    if(condition.kind==='exists') {
      const matched=(matches.length===1)===condition.value;
      return out(matched?'pass':'fail',matched?'matched':'mismatch');
    }
    if(matches.length===0) return out('inconclusive','missing');
    const el=matches[0];
    let actual;
    if(['text','value','selected_values'].includes(condition.kind) && sensitive(el))
      return out('inconclusive','sensitive');
    switch(condition.kind) {
      case 'text':
        if(typeof el.innerText!=='string') return out('inconclusive','unsupported_element');
        actual=el.innerText; break;
      case 'value':
        if(!['INPUT','TEXTAREA','SELECT'].includes(el.tagName) || el.type==='file')
          return out('inconclusive','unsupported_element');
        actual=el.value; break;
      case 'checked':
        if(el.tagName!=='INPUT' || !['checkbox','radio'].includes(el.type))
          return out('inconclusive','unsupported_element');
        actual=el.checked; break;
      case 'selected_values':
        if(el.tagName!=='SELECT') return out('inconclusive','unsupported_element');
        actual=Array.from(el.selectedOptions, option=>option.value); break;
      case 'visible': actual=visible(el); break;
      default: return out('inconclusive','unsupported_element');
    }
    const matched=Array.isArray(actual)
      ? actual.length===condition.value.length && actual.every((value,index)=>value===condition.value[index])
      : actual===condition.value;
    // No observed text, values, URLs, selectors or exception strings leave this
    // readback. Comparisons of non-secret fields happen within the fixed script.
    return out(matched?'pass':'fail',matched?'matched':'mismatch');
  });
}"""


class VerifierRegistry:
    def __init__(self, *, timeout_seconds: float = 3, clock: Callable[[], datetime] = utc_now):
        if not 0 < timeout_seconds <= 10:
            raise ValueError("Verifier deadline must be within ten seconds")
        self.timeout_seconds, self.clock = timeout_seconds, clock
        self._registered: dict[str, _Registered] = {}
        self._lock = threading.RLock()

    def _register(self, task: Task, conditions, verifier_id: str, callback=None) -> BrowserVerifierSpec:
        if not isinstance(task, Task):
            raise TypeError("The parent must register a typed Task")
        task = Task.model_validate_json(task.model_dump_json())
        if any(not isinstance(condition, BrowserCondition) for condition in conditions):
            raise TypeError("The parent must register typed BrowserConditions")
        spec = BrowserVerifierSpec(task_id=task.id, verifier_id=verifier_id,
            mode="callback" if callback is not None else "dom", criteria=task.success_criteria,
            conditions=tuple(sorted(conditions, key=lambda condition: condition.id)))
        spec = BrowserVerifierSpec.model_validate_json(spec.model_dump_json())
        entry = _Registered(spec.model_dump_json(), callback)
        with self._lock:
            previous = self._registered.get(task.id)
            if previous is not None and (previous.serialized != entry.serialized or previous.callback is not callback):
                raise ValueError("Verifier specs are immutable for each task")
            self._registered[task.id] = entry
        return spec

    def register(self, task: Task, conditions: tuple[BrowserCondition, ...], *,
                 verifier_id: str = "browser.readback.v1") -> BrowserVerifierSpec:
        return self._register(task, tuple(conditions), verifier_id)

    def register_callback(self, task: Task, callback: ReadbackCallback, *, verifier_id: str) -> BrowserVerifierSpec:
        """Register trusted async parent code, never model-supplied Python/JS.

        This is not a read-only capability sandbox: trusted callbacks receive the
        driver to read their original oracle. They must not send input, change
        focus or return observed secrets. The registry validates result coverage
        and checks that the current page/document/URL survived the readback.
        """
        if not callable(callback) or not (inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(getattr(callback, "__call__", None))):
            raise TypeError("Verifier callback must be a trusted async callable")
        return self._register(task, (), verifier_id, callback)

    def spec_for(self, task_id: str) -> BrowserVerifierSpec:
        with self._lock:
            entry = self._registered.get(task_id)
        if entry is None:
            raise LookupError("No parent verifier is registered for this task")
        return BrowserVerifierSpec.model_validate_json(entry.serialized)

    def _inconclusive(self, spec: BrowserVerifierSpec, reason: Reason) -> tuple[CheckResult, ...]:
        observed_at = self.clock()
        return tuple(CheckResult(criterion_id=criterion.id, status="inconclusive", reason=reason,
                                 observed_at=observed_at) for criterion in spec.criteria)

    async def verify(self, task_id: str, driver: BrowserDriver) -> tuple[CheckResult, ...]:
        spec = self.spec_for(task_id)
        entry = self._registered[task_id]
        if not isinstance(driver, BrowserDriver) or driver.headless is not True:
            return self._inconclusive(spec, "unsupported_driver")
        page = driver._page
        if driver._context is None or page is None or page.is_closed() or page not in driver._context.pages or driver._closing:
            return self._inconclusive(spec, "no_page")
        if driver._actions:
            return self._inconclusive(spec, "busy_driver")
        if driver.allowed_origin is not None:
            try:
                if BrowserDriver._http_origin(page.url) != BrowserDriver._http_origin(driver.allowed_origin):
                    return self._inconclusive(spec, "outside_scope")
            except ValueError:
                return self._inconclusive(spec, "outside_scope")

        async def readback():
            url = page.url
            document = await page.evaluate_handle("() => document")
            try:
                if entry.callback is not None:
                    returned = await entry.callback(driver)
                    if not isinstance(returned, tuple) or any(not isinstance(item, CheckResult) for item in returned):
                        return self._inconclusive(spec, "invalid_callback")
                    checked = tuple(CheckResult.model_validate_json(item.model_dump_json()) for item in returned)
                    if len(checked) != len(spec.criteria) or {item.criterion_id for item in checked} != {criterion.id for criterion in spec.criteria}:
                        return self._inconclusive(spec, "invalid_callback")
                    by_criterion = {item.criterion_id: item for item in checked}
                    results = tuple(by_criterion[criterion.id] for criterion in spec.criteria)
                else:
                    dom = [condition for condition in spec.conditions if not condition.kind.startswith("url_")]
                    raw = await document.evaluate(_READ_CONDITIONS, [condition.model_dump(mode="json") for condition in dom])
                    reads = {item.condition_id: item for item in map(ConditionResult.model_validate, raw)}
                    if len(reads) != len(dom) or set(reads) != {condition.id for condition in dom}:
                        return self._inconclusive(spec, "read_failed")
                    for condition in spec.conditions:
                        if condition.kind.startswith("url_"):
                            matched = (url == condition.value if condition.kind == "url_exact" else
                                       BrowserDriver._http_origin(url) == BrowserDriver._http_origin(condition.value))
                            reads[condition.id] = ConditionResult(condition_id=condition.id,
                                status="pass" if matched else "fail", reason="matched" if matched else "mismatch")
                    results = []
                    for criterion in spec.criteria:
                        checks = tuple(reads[condition.id] for condition in spec.conditions if condition.criterion_id == criterion.id)
                        status = "fail" if any(check.status == "fail" for check in checks) else (
                            "inconclusive" if any(check.status == "inconclusive" for check in checks) else "pass")
                        results.append(CheckResult(criterion_id=criterion.id, status=status,
                            reason={"pass":"conditions_passed", "fail":"condition_failed", "inconclusive":"condition_inconclusive"}[status],
                            condition_results=checks))
                same_document = await document.evaluate("doc => doc === document")
                # Recheck Python-side state after the final IPC await as well;
                # a concurrent tab switch need not destroy the old document.
                if (not same_document or driver._closing or driver._actions or driver._page is not page
                        or page.is_closed() or page.url != url or driver.headless is not True
                        or driver._context is None or page not in driver._context.pages):
                    return self._inconclusive(spec, "page_changed")
                observed_at = self.clock()
                return tuple(CheckResult.model_validate(item.model_dump() | {"observed_at": observed_at}) for item in results)
            finally:
                try:
                    await asyncio.wait_for(document.dispose(), timeout=.25)
                except Exception:
                    pass

        try:
            return await asyncio.wait_for(readback(), timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            return self._inconclusive(spec, "timeout")
        except Exception:
            return self._inconclusive(spec, "read_failed")
