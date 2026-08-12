"""Opt-in Azure Monitor / Application Insights telemetry (OpenTelemetry).

Sovereignty contract
--------------------
Lumena is sovereign-by-default: it never exports data off the host unless
explicitly configured. Azure Monitor export therefore requires BOTH:

1. ``APPLICATIONINSIGHTS_CONNECTION_STRING`` set in the environment (the
   standard Azure convention), AND
2. ``LUMENA_SOVEREIGN=false`` — enabling export while sovereign mode is on
   raises ``SovereignViolationError`` instead of silently leaking traces.

Failure honesty: if the connection string is set but the
``azure-monitor-opentelemetry`` package is missing, startup FAILS with an
actionable error rather than silently running without the telemetry the
operator asked for. Install with ``pip install "lumena[azure]"``.

What is exported when enabled: FastAPI request spans and the structlog
messages routed through stdlib logging. Memory CONTENT is never attached
to spans by Lumena's instrumentation.
"""

from __future__ import annotations

import os

from lumena.logging import get_console_logger

logger = get_console_logger(__name__)

_CONN_ENV = "APPLICATIONINSIGHTS_CONNECTION_STRING"


def configure_azure_monitor_if_enabled(config, app=None) -> bool:
    """Configure Azure Monitor OpenTelemetry export if explicitly requested.

    Args:
        config: LumenaConfig — used to enforce the sovereign boundary.
        app: Optional FastAPI app to instrument with request spans.

    Returns:
        True if telemetry export was configured, False if not requested.

    Raises:
        SovereignViolationError: Connection string set while sovereign mode
            is enabled.
        RuntimeError: Connection string set but the azure extra is not
            installed (never silently skip telemetry the operator enabled).
    """
    conn = os.environ.get(_CONN_ENV, "").strip()
    if not conn:
        logger.debug("azure_monitor_not_configured")
        return False

    # Exporting traces/logs to Azure is an external network call: the
    # operator must explicitly leave sovereign mode to enable it.
    config.assert_sovereign("azure_monitor_telemetry_export")

    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
    except ImportError as exc:
        raise RuntimeError(
            f"{_CONN_ENV} is set but the 'azure-monitor-opentelemetry' "
            "package is not installed. Install it with: "
            'pip install "lumena[azure]" — refusing to start without the '
            "telemetry you configured."
        ) from exc

    configure_azure_monitor(connection_string=conn)

    if app is not None:
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(app)
        except ImportError as exc:
            raise RuntimeError(
                "azure-monitor-opentelemetry is installed but "
                "opentelemetry-instrumentation-fastapi is missing; install "
                'the full extra: pip install "lumena[azure]"'
            ) from exc

    logger.info("azure_monitor_configured", instrumented=app is not None)
    return True
