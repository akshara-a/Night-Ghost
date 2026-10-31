"""Payment module with business logic for charges, refunds, and discounts."""

def round_money(amount: float) -> float:
    """Round currency amount to two decimal places."""
    return round(amount, 2)


def charge(amount: float, currency: str = "USD") -> dict:
    """Process a charge for a given amount and currency."""
    if amount <= 0:
        raise ValueError("Charge amount must be positive.")
    rounded = round_money(amount)
    return {
        "status": "success",
        "amount": rounded,
        "currency": currency.upper(),
        "transaction_id": f"tx_{int(amount * 100)}",
    }


def refund(amount: float, transaction_id: str) -> dict:
    """Issue a refund for an existing transaction."""
    if amount <= 0:
        raise ValueError("Refund amount must be positive.")
    if not transaction_id or not transaction_id.startswith("tx_"):
        raise ValueError("Invalid transaction ID format.")
    return {
        "status": "refunded",
        "amount": round_money(amount),
        "transaction_id": transaction_id,
    }


def apply_discount(amount: float, discount_pct: float) -> float:
    """Apply percentage discount to an amount."""
    if discount_pct < 0 or discount_pct > 100:
        raise ValueError("Discount percentage must be between 0 and 100.")
    discount = amount * (discount_pct / 100.0)
    return round_money(amount - discount)
