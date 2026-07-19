"""OTel tracing setup — opt-in via OTLP_ENDPOINT env var.

When OTLP_ENDPOINT is set, configures a TracerProvider exporting to Jaeger
(or any OTLP-compatible backend) and auto-instruments the FastAPI app so every
request gets a span. When the variable is absent the function is a no-op and
the platform runs exactly as before.
"""

from __future__ import annotations

import os


def setup_tracing(app, service_name: str = "orchestra") -> None:
    """Instrument *app* with OTel FastAPI auto-instrumentation.

    Reads OTLP_ENDPOINT from env (e.g. http://localhost:4318).
    No-op when the variable is unset or empty.
    """
    endpoint = os.getenv("OTLP_ENDPOINT", "").rstrip("/")
    if not endpoint:
        return

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app)
