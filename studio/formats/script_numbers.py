"""Compact decimal text for numeric game script parameters."""


from decimal import Decimal, ROUND_HALF_UP, localcontext


def format_number(value: float, decimals: int = 2) -> str:
    # Match QDoubleSpinBox at exact midpoints, including negative coordinates.
    with localcontext() as context:
        context.rounding = ROUND_HALF_UP
        text = format(Decimal.from_float(float(value)), f".{decimals}f")
    if decimals:
        text = text.rstrip("0").rstrip(".")
    return "0" if text == "-0" else text
