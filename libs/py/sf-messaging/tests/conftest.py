"""Installs an in-memory metric reader on the global meter provider.

Runs at collection time, before any test, so sf_messaging's proxy
instruments bind to it. Readings are cumulative across the session: tests
filter by a unique ``service``/``consumer`` label, never by totals.
"""

import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

_reader = InMemoryMetricReader()
metrics.set_meter_provider(MeterProvider(metric_readers=[_reader]))


def metric_value(name: str, **labels: str) -> float:
    """Counter: summed value. Histogram: summed sample count. Gauge: value.
    Over every data point whose attributes include ``labels``."""
    data = _reader.get_metrics_data()
    total = 0.0
    for resource_metrics in data.resource_metrics if data else []:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != name:
                    continue
                for point in metric.data.data_points:
                    attrs = dict(point.attributes or {})
                    if all(attrs.get(k) == v for k, v in labels.items()):
                        total += getattr(point, "count", None) or getattr(point, "value", 0)
    return total


@pytest.fixture
def read_metric():  # type: ignore[no-untyped-def]
    return metric_value
