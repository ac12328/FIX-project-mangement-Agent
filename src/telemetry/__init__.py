"""OpenTelemetry setup and traced decorator."""
from src.telemetry.setup import init_telemetry
from src.telemetry.decorators import traced

__all__ = ["init_telemetry", "traced"]
