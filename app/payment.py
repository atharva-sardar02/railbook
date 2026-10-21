"""Mock payment provider. Needs an API key (from Vault) like a real one would."""

from decimal import Decimal

from app.config import get_settings


class PaymentError(Exception):
    pass


def charge(amount: Decimal, simulate_failure: bool = False) -> None:
    if not get_settings().payment_api_key.get_secret_value():
        raise PaymentError("payment provider not configured")
    if simulate_failure:
        raise PaymentError("payment declined")
