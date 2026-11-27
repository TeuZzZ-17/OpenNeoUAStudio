"""Compact decimal text for numeric script parameters and the engine's boolean words."""


from decimal import Decimal, ROUND_HALF_UP, localcontext


def parse_bool(text: str) -> bool | None:
    """Read a yes/no value the way the engine does (StrParseBool): yes/true/on, no/false/off
    or a whole number (non-zero = true). None when the text is none of these."""
    word = text.strip().casefold()
    if word in ("yes", "true", "on"):
        return True
    if word in ("no", "false", "off"):
        return False
    try:
        # strtol base 0 also reads a leading-zero number as octal ("01" = on).
        octal = len(word.lstrip("+-")) > 1 and word.lstrip("+-")[0] == "0" and word.lstrip("+-").isdigit()
        return int(word, 8 if octal else 0) != 0
    except ValueError:
        return None


def format_number(value: float, decimals: int = 2) -> str:
    # Match QDoubleSpinBox at exact midpoints, including negative coordinates.
    with localcontext() as context:
        context.rounding = ROUND_HALF_UP
        text = format(Decimal.from_float(float(value)), f".{decimals}f")
    if decimals:
        text = text.rstrip("0").rstrip(".")
    return "0" if text == "-0" else text
