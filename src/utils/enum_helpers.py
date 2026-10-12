"""Shared enum helpers — avoids repeated hasattr(x, 'value') checks."""


def enum_val(x) -> str:
    """Extract string value from a Pydantic/Python enum, or coerce to str."""
    if x is None:
        return ""
    return x.value if hasattr(x, "value") else str(x)
