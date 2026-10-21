"""Process-local relay metrics, rendered in the Prometheus text exposition format.

Hand-written rather than pulled from prometheus_client: the relay needs a
handful of counters, a few gauges and one histogram, and a dependency for that
would be one more thing to pin in an image that is meant to stay small. The
format is plain text (version 0.0.4) and stable, so any Prometheus-compatible
scraper reads it.

Like the rate limiters, this state lives in one process: with several relay
instances each reports its own share, which is what a scraper expects.
"""

from __future__ import annotations

import bisect
import math
import threading
from collections.abc import Callable, Iterable, Sequence

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# Label values every labelled counter starts with, so a dashboard sees an
# explicit 0 instead of a missing series before the first event of each kind.
EDIT_DECISIONS = ("allow", "warn", "block")
WS_REJECTION_REASONS = ("rate_limited", "room_not_found", "version", "name")
DECISION_SCOPES = ("symbol", "lines", "file", "intent", "plan", "branch")

# Edit checks sit on an agent's blocking hook with a 1.5 s client budget; the
# interesting range is well under that, so the buckets are fine-grained low.
EDIT_CHECK_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25)


def _format_value(value: float) -> str:
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if math.isnan(value):
        return "NaN"
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(pairs: Iterable[tuple[str, str]]) -> str:
    body = ",".join(f'{key}="{_escape_label(value)}"' for key, value in pairs)
    return f"{{{body}}}" if body else ""


LabelValue = str | tuple[str, ...]


class Counter:
    """Monotonic counter, pre-seeded with known label values.

    `label` is one label name or a tuple of names; with several names each
    label value passed to inc/value is a tuple in the same order.
    """

    def __init__(
        self,
        name: str,
        help_text: str,
        label: str | tuple[str, ...] | None = None,
        values: Sequence[LabelValue] = (),
    ) -> None:
        self.name = name
        self.help_text = help_text
        self.label = label
        self._names: tuple[str, ...] = (
            () if label is None else (label,) if isinstance(label, str) else tuple(label)
        )
        self._initial = tuple(values) if label else ("",)
        self._counts: dict[LabelValue, float] = {}
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._counts = {value: 0.0 for value in self._initial}

    def inc(self, label_value: LabelValue = "", amount: float = 1.0) -> None:
        with self._lock:
            self._counts[label_value] = self._counts.get(label_value, 0.0) + amount

    def value(self, label_value: LabelValue = "") -> float:
        with self._lock:
            return self._counts.get(label_value, 0.0)

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} counter"]
        with self._lock:
            items = sorted(self._counts.items(), key=lambda item: str(item[0]))
        for label_value, count in items:
            values = label_value if isinstance(label_value, tuple) else (label_value,)
            labels = _labels(zip(self._names, values)) if self._names else ""
            lines.append(f"{self.name}{labels} {_format_value(count)}")
        return lines

class Gauge:
    """Gauge read from a callback at scrape time, so it can never go stale."""

    def __init__(self, name: str, help_text: str, read: Callable[[], float]) -> None:
        self.name = name
        self.help_text = help_text
        self._read = read

    def render(self) -> list[str]:
        return [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} gauge",
            f"{self.name} {_format_value(float(self._read()))}",
        ]


class Histogram:
    """Cumulative-bucket histogram; +Inf is implicit and always present."""

    def __init__(self, name: str, help_text: str, buckets: Sequence[float]) -> None:
        self.name = name
        self.help_text = help_text
        self.buckets = tuple(sorted(buckets))
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            # One slot per finite bucket plus the overflow slot for +Inf.
            self._slots = [0] * (len(self.buckets) + 1)
            self._sum = 0.0
            self._count = 0

    def observe(self, value: float) -> None:
        # bisect_left keeps the upper bound inclusive (le = "less or equal").
        slot = bisect.bisect_left(self.buckets, value)
        with self._lock:
            self._slots[slot] += 1
            self._sum += value
            self._count += 1

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    def render(self) -> list[str]:
        with self._lock:
            slots = list(self._slots)
            total, count = self._sum, self._count
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} histogram"]
        cumulative = 0
        for bound, hits in zip((*self.buckets, math.inf), slots):
            cumulative += hits
            labels = _labels([("le", _format_value(bound))])
            lines.append(f"{self.name}_bucket{labels} {cumulative}")
        lines.append(f"{self.name}_sum {_format_value(total)}")
        lines.append(f"{self.name}_count {count}")
        return lines


class Registry:
    def __init__(self) -> None:
        self._metrics: list[Counter | Gauge | Histogram] = []

    def register(self, metric: Counter | Gauge | Histogram) -> None:
        self._metrics.append(metric)

    def unregister(self, name: str) -> None:
        self._metrics = [metric for metric in self._metrics if metric.name != name]

    def reset(self) -> None:
        """Zero every counter and histogram. Tests only: a scraper treats a
        counter that goes backwards as a process restart."""
        for metric in self._metrics:
            if isinstance(metric, (Counter, Histogram)):
                metric.reset()

    def render(self) -> str:
        lines: list[str] = []
        for metric in self._metrics:
            lines.extend(metric.render())
        return "\n".join(lines) + "\n"


registry = Registry()

edit_checks_total = Counter(
    "synergo_edit_checks_total",
    "Edit checks answered, by decision and the scope that decided it.",
    label=("decision", "scope"),
    values=[(d, sc) for d in EDIT_DECISIONS for sc in DECISION_SCOPES],
)
decision_feedback_total = Counter(
    "synergo_decision_feedback_total",
    "Dashboard votes on warn/block decisions: useful or noise, by scope.",
    label=("useful", "scope"),
    values=[(u, sc) for u in ("true", "false") for sc in DECISION_SCOPES],
)
activity_messages_total = Counter(
    "synergo_activity_messages_total",
    "Activity messages accepted (valid and not rate-limited).",
)
intents_total = Counter(
    "synergo_intents_total",
    "Intent messages accepted (valid and not rate-limited).",
)
ws_rejections_total = Counter(
    "synergo_ws_rejections_total",
    "WebSocket connections or join attempts refused, by reason.",
    label="reason",
    values=WS_REJECTION_REASONS,
)
edit_check_seconds = Histogram(
    "synergo_edit_check_seconds",
    "Server-side time to decide an edit check, from receipt to reply ready.",
    EDIT_CHECK_BUCKETS,
)

for _metric in (
    edit_checks_total,
    activity_messages_total,
    intents_total,
    ws_rejections_total,
    edit_check_seconds,
    decision_feedback_total,
):
    registry.register(_metric)


def register_gauge(name: str, help_text: str, read: Callable[[], float]) -> None:
    """Add (or replace) a scrape-time gauge. Replacing keeps re-imports and
    test reloads from listing one gauge twice."""
    registry.unregister(name)
    registry.register(Gauge(name, help_text, read))


def render() -> str:
    return registry.render()
