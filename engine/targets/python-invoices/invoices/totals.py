"""Invoice totals for the Contoso billing service."""


def line_total(unit_price: str, quantity: int) -> float:
    return float(unit_price) * quantity


def invoice_total(lines: list[tuple[str, int]], tax_rate: str = "0") -> str:
    """Return the invoice total including tax, rounded half-up to whole cents."""
    subtotal = sum(line_total(price, quantity) for price, quantity in lines)
    total = subtotal * (1 + float(tax_rate))
    return f"{round(total, 2):.2f}"
