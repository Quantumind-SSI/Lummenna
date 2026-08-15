"""In-process metrics registry with Prometheus text exposition.

Dependency-free, thread-safe operational metrics for the API server:
request counters, latency histograms, and error counters, rendered in the
Prometheus exposition format (text/plain; version=0.0.4) at
``/metrics/prometheus`` for scraping by Prometheus, Azure Monitor managed
Prometheus, or any compatible agent.

Design constraints:
- No third-party dependency (prometheus_client is NOT required); the
  exposition format is stable, documented plain text.
- Bounded memory: label cardinality is capped (route template x method x
  status class), histogram buckets are fixed at construction.
- Thread-safe: a single lock guards all mutation; observation is O(#buckets).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field

# Latency buckets in seconds — tuned for a local-first API where the hot
# path is single-digit milliseconds and the slow path is model inference.
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

_MAX_LABEL_SETS = 512  # hard cap against label-cardinality explosion


@dataclass
class _Histogram:
    buckets: tuple[float, ...]
    counts: list[int] = field(default_factory=list)
    total_sum: float = 0.0
    total_count: int = 0

    def __post_init__(self) -> None:
        if not self.counts:
            self.counts = [0] * (len(self.buckets) + 1)  # +Inf bucket

    def observe(self, value: float) -> None:
        self.total_sum += value
        self.total_count += 1
        for i, bound in enumerate(self.buckets):
            if value <= bound:
                self.counts[i] += 1
                return
        self.counts[-1] += 1


class MetricsRegistry:
    """Thread-safe counter + histogram registry with Prometheus rendering."""

    def __init__(self, buckets: tuple[float, ...] = DEFAULT_BUCKETS) -> None:
        self._lock = threading.Lock()
        self._buckets = buckets
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = defaultdict(int)
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], _Histogram] = {}
        self._started_at = time.time()

    @staticmethod
    def _key(name: str, labels: dict[str, str] | None) -> tuple[str, tuple[tuple[str, str], ...]]:
        return (name, tuple(sorted((labels or {}).items())))

    def inc(self, name: str, labels: dict[str, str] | None = None, value: int = 1) -> None:
        """Increment a counter; silently drops NEW label sets past the cap
        (existing series keep counting — bounded memory beats completeness)."""
        key = self._key(name, labels)
        with self._lock:
            if key not in self._counters and len(self._counters) >= _MAX_LABEL_SETS:
                return
            self._counters[key] += value

    def observe(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        """Record a histogram observation (seconds for latency metrics)."""
        key = self._key(name, labels)
        with self._lock:
            hist = self._histograms.get(key)
            if hist is None:
                if len(self._histograms) >= _MAX_LABEL_SETS:
                    return
                hist = _Histogram(buckets=self._buckets)
                self._histograms[key] = hist
            hist.observe(value)

    @staticmethod
    def _fmt_labels(labels: tuple[tuple[str, str], ...], extra: str = "") -> str:
        parts = [f'{k}="{v}"' for k, v in labels]
        if extra:
            parts.append(extra)
        return "{" + ",".join(parts) + "}" if parts else ""

    def render(self, gauges: dict[str, float | int | None] | None = None) -> str:
        """Render everything in Prometheus text exposition format.

        Args:
            gauges: Optional point-in-time gauge values (e.g. chunk counts)
                appended as ``gauge`` metrics; ``None`` values are skipped
                (unmeasured is never rendered as zero).
        """
        lines: list[str] = []
        with self._lock:
            counters = dict(self._counters)
            histograms = {
                k: (_h.buckets, list(_h.counts), _h.total_sum, _h.total_count)
                for k, _h in self._histograms.items()
            }

        seen_counter_names = set()
        for (name, labels), value in sorted(counters.items()):
            if name not in seen_counter_names:
                lines.append(f"# TYPE {name} counter")
                seen_counter_names.add(name)
            lines.append(f"{name}{self._fmt_labels(labels)} {value}")

        seen_hist_names = set()
        for (name, labels), (buckets, counts, total_sum, total_count) in sorted(histograms.items()):
            if name not in seen_hist_names:
                lines.append(f"# TYPE {name} histogram")
                seen_hist_names.add(name)
            cumulative = 0
            for bound, count in zip(buckets, counts[:-1], strict=True):
                cumulative += count
                le_label = 'le="' + str(bound) + '"'
                lines.append(f"{name}_bucket{self._fmt_labels(labels, le_label)} {cumulative}")
            cumulative += counts[-1]
            inf_label = 'le="+Inf"'
            lines.append(f"{name}_bucket{self._fmt_labels(labels, inf_label)} {cumulative}")
            lines.append(f"{name}_sum{self._fmt_labels(labels)} {total_sum:.6f}")
            lines.append(f"{name}_count{self._fmt_labels(labels)} {total_count}")

        lines.append("# TYPE lumena_uptime_seconds gauge")
        lines.append(f"lumena_uptime_seconds {time.time() - self._started_at:.1f}")

        for gname, gvalue in sorted((gauges or {}).items()):
            if gvalue is None:
                continue  # honest: unmeasured != zero
            lines.append(f"# TYPE {gname} gauge")
            lines.append(f"{gname} {gvalue}")

        return "\n".join(lines) + "\n"


# Module-level registry used by the API server middleware and endpoints.
registry = MetricsRegistry()


def normalize_route(path: str) -> str:
    """Collapse request paths to bounded route templates.

    Prevents label-cardinality explosion from arbitrary paths: known routes
    pass through, anything else becomes ``other``.
    """
    known = {
        "/health",
        "/ready",
        "/dashboard",
        "/metrics",
        "/metrics/prometheus",
        "/v1/status",
        "/v1/search",
        "/v1/store",
        "/v1/feedback",
        "/v1/assemble",
        "/v1/turn",
        "/v1/dashboard-data",
    }
    return path if path in known else "other"
