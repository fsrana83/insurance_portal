"""Payment gateway abstraction.

Only the **demo gateway** is implemented. It never moves money: it validates a card number with the Luhn check and decides the
outcome from fixed test numbers, so the whole online-payment journey (link -> checkout page -> receipt -> accounting) can be
exercised end to end. To plug in a real PSP (e.g. Thawani, Amazon Payment Services, Network International, Stripe), subclass
``BaseGateway``, implement ``charge`` (or a redirect / webhook flow), register it in ``GATEWAYS`` and set PAYMENT_GATEWAY.
Never store full card numbers or CVV: only the last four digits are kept."""
import re
import secrets
from dataclasses import dataclass
from datetime import date

from django.conf import settings


@dataclass
class GatewayResult:
    success: bool
    txn_id: str = ""
    message: str = ""
    last4: str = ""


def luhn_ok(number):
    digits = [int(c) for c in number][::-1]
    total = sum(d if i % 2 == 0 else (d * 2 - 9 if d * 2 > 9 else d * 2) for i, d in enumerate(digits))
    return total % 10 == 0


class BaseGateway:
    name = "base"
    label = "Gateway"
    is_test = False

    def charge(self, payment, number, expiry, cvv, holder):  # pragma: no cover - interface
        raise NotImplementedError


class DemoGateway(BaseGateway):
    name = "demo"
    label = "Demo payment gateway (test mode, no real money moves)"
    is_test = True
    TEST_CARDS = [
        ("4242 4242 4242 4242", "Succeeds"),
        ("4000 0000 0000 0002", "Declined by the bank"),
        ("4000 0000 0000 9995", "Insufficient funds"),
        ("4000 0000 0000 0069", "Expired card"),
    ]
    _outcomes = {"4000000000000002": "Card declined by issuer.", "4000000000000069": "Card expired.",
                 "4000000000009995": "Insufficient funds."}

    def charge(self, payment, number, expiry, cvv, holder):
        digits = re.sub(r"\D", "", number or "")
        if len(digits) < 13 or len(digits) > 19 or not luhn_ok(digits):
            return GatewayResult(False, message="The card number is not valid.")
        m = re.match(r"^(\d{2})\s*/\s*(\d{2,4})$", (expiry or "").strip())
        if not m:
            return GatewayResult(False, message="Enter the expiry as MM/YY.")
        mm, yy = int(m.group(1)), int(m.group(2))
        yy += 2000 if yy < 100 else 0
        if not 1 <= mm <= 12 or (yy, mm) < (date.today().year, date.today().month):
            return GatewayResult(False, message="The card has expired.")
        if not re.match(r"^\d{3,4}$", cvv or ""):
            return GatewayResult(False, message="Enter the 3 or 4 digit security code.")
        if digits in self._outcomes:
            return GatewayResult(False, message=self._outcomes[digits], last4=digits[-4:])
        return GatewayResult(True, txn_id="DEMO-" + secrets.token_hex(5).upper(), message="Approved", last4=digits[-4:])


GATEWAYS = {"demo": DemoGateway()}


def get_gateway(name=None):
    return GATEWAYS[name or settings.PAYMENT_GATEWAY]
