from django import forms

from core.widgets import MoneyInput
from motor.forms import StyledMixin

from .models import Payment


class _Base(StyledMixin, forms.Form):
    def __init__(self, *a, currency=None, **kw):
        super().__init__(*a, **kw)
        if currency is not None:
            self.fields["amount"].widget.attrs["data-currency-fixed"] = currency.code
        self._style()


class ReceiptForm(_Base):
    amount = forms.DecimalField(min_value=0, decimal_places=3, widget=MoneyInput)
    method = forms.ChoiceField(choices=[m for m in Payment.METHODS if m[0] != Payment.ONLINE])
    reference = forms.CharField(max_length=60, required=False, label="Cheque / transfer / POS reference")
    payer_name = forms.CharField(max_length=100, required=False, label="Received from (if not the insured)")
    narration = forms.CharField(max_length=160, required=False)


class RequestForm(_Base):
    amount = forms.DecimalField(min_value=0, decimal_places=3, widget=MoneyInput)
    narration = forms.CharField(max_length=160, required=False, label="Note shown on the payment page")


class CheckoutForm(forms.Form):
    holder = forms.CharField(max_length=100, label="Name on card")
    number = forms.CharField(max_length=23, label="Card number")
    expiry = forms.CharField(max_length=7, label="Expiry (MM/YY)")
    cvv = forms.CharField(max_length=4, label="CVV", widget=forms.PasswordInput(render_value=False))

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        for f in self.fields.values():
            f.widget.attrs.setdefault("class", "input")
            f.widget.attrs["autocomplete"] = "off"
