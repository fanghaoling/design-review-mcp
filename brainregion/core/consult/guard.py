"""Input guardrails for external consultation.

The consult path can send user-supplied snippets to third-party model endpoints, so
it needs a small hard gate before prompt rendering: validate required fields,
redact common secrets, and cap total outbound text size.
"""
from __future__ import annotations

import re
from dataclasses import replace

from .report import ConsultRequest

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd)\b\s*[:=]\s*['\"]?([^\s'\";]+)"
        ),
        r"\1=[REDACTED]",
    ),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"), "Bearer [REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9._-]{12,}\b"), "sk-[REDACTED]"),
]


def _as_str_list(values: list[str] | None) -> list[str]:
    if not values:
        return []
    return [str(v) for v in values if str(v).strip()]


def _redact(text: str) -> tuple[str, int]:
    out = text or ""
    count = 0
    for pattern, repl in _SECRET_PATTERNS:
        out, n = pattern.subn(repl, out)
        count += n
    return out, count


def redact_text(text: str) -> tuple[str, int]:
    """Redact common secret-looking values from diagnostic text."""
    return _redact(text)


_DIAG_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\s*(.*?)```", re.IGNORECASE | re.DOTALL)


def summarize_unparseable_output(content: str, *, max_excerpt: int = 700) -> dict:
    """Return a small, redacted diagnostic payload for parse failures.

    Shared by consult/planner engines when a model returns output that cannot be
    parsed as the expected JSON object. ``has_object_start`` tells whether the
    model emitted any ``{`` at all: False points at reasoning-token starvation
    (max_tokens too low for a thinking model) or a format miss, rather than a
    truncation mid-object. Excerpts are redacted so failed_models never leaks
    secrets that may have been echoed back from the prompt.
    """
    raw = content or ""
    redacted, redacted_items = redact_text(raw)
    excerpt = " ".join(redacted.split())
    return {
        "content_chars": len(raw),
        "excerpt_chars": min(len(excerpt), max_excerpt),
        "output_excerpt": excerpt[:max_excerpt],
        "redacted_items": redacted_items,
        "fenced_blocks": len(_DIAG_FENCE_RE.findall(raw)),
        "has_object_start": "{" in raw,
        "has_array_start": "[" in raw,
    }


def _take(text: str, *, field: str, remaining: int, meta: dict) -> tuple[str, int]:
    if remaining <= 0:
        if text:
            meta["truncated_fields"].append(field)
        return "", 0
    if len(text) <= remaining:
        return text, len(text)
    meta["truncated_fields"].append(field)
    marker = "\n[TRUNCATED: input budget exhausted]"
    keep = max(0, remaining - len(marker))
    return text[:keep] + marker, remaining


def prepare_request(request: ConsultRequest, max_input_chars: int = 24000) -> tuple[ConsultRequest, dict]:
    """Return a sanitized request and guard metadata.

    ``max_input_chars`` is a coarse character budget for all outbound user content.
    It keeps the feature predictable without introducing tokenizer-specific
    dependencies into the core path.
    """
    if not (request.problem or "").strip():
        raise ValueError("problem 不能为空")
    if max_input_chars <= 0:
        raise ValueError("max_input_chars 必须大于 0")

    meta = {
        "max_input_chars": int(max_input_chars),
        "input_chars": 0,
        "sent_chars": 0,
        "redacted_items": 0,
        "truncated_fields": [],
    }
    remaining = int(max_input_chars)

    def clean_field(value: str, field: str) -> str:
        nonlocal remaining
        redacted, n = _redact(str(value or ""))
        meta["redacted_items"] += n
        meta["input_chars"] += len(str(value or ""))
        taken, used = _take(redacted, field=field, remaining=remaining, meta=meta)
        remaining -= used
        meta["sent_chars"] += len(taken)
        return taken

    attempts = [clean_field(v, f"attempts[{idx}]") for idx, v in enumerate(_as_str_list(request.attempts))]
    constraints = [clean_field(v, f"constraints[{idx}]") for idx, v in enumerate(_as_str_list(request.constraints))]

    files: dict[str, str] = {}
    for path, content in (request.files or {}).items():
        safe_path = clean_field(str(path), f"files[{path!r}].path")
        files[safe_path] = clean_field(str(content), f"files[{path!r}].content")

    sanitized = replace(
        request,
        problem=clean_field(request.problem, "problem"),
        context=clean_field(request.context, "context"),
        files=files,
        logs=clean_field(request.logs, "logs"),
        attempts=attempts,
        goal=clean_field(request.goal, "goal"),
        current_attempt=clean_field(request.current_attempt, "current_attempt"),
        why_stuck=clean_field(request.why_stuck, "why_stuck"),
        question=clean_field(request.question, "question"),
        desired_output=clean_field(request.desired_output, "desired_output"),
        constraints=constraints,
    )
    meta["truncated_fields"] = sorted(set(meta["truncated_fields"]))
    return sanitized, meta
