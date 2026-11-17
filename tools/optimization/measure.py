"""Measurement arithmetic shared by the API report and its tests."""
import math
import statistics


def summarize(samples: list[float]) -> dict:
    if not samples:
        return {"count": 0, "median_ms": None, "p95_ms": None, "max_ms": None}
    ordered = sorted(samples)
    return {
        "count": len(ordered),
        "median_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[math.ceil(0.95 * len(ordered)) - 1], 3),
        "max_ms": round(ordered[-1], 3),
    }
