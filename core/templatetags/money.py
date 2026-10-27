"""Money rendering with currency symbols.

The Omani rial sign is U+20C4 (Unicode 18, Sept 2026). Many fonts do not carry it yet, so the
markup keeps a fallback (ISO code, or an SVG dropped into static/img/currency/<CODE>.svg) and
app.js swaps it in automatically on devices that cannot draw the glyph.
"""
from decimal import Decimal

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static
from django.utils.html import format_html

from core.models import Currency

register = template.Library()
_cache = {}


def _currency(value):
    if isinstance(value, Currency):
        return value
    if value not in _cache:
        _cache[value] = Currency.objects.filter(code=value).first()
    return _cache[value]


def clear_cache():
    _cache.clear()


def _number(amount, cur):
    if amount is None or amount == "":
        return "—"
    amount = cur.quantize(Decimal(amount))
    return f"{amount:,.{cur.decimals}f}"


@register.filter
def money(amount, currency):
    cur = _currency(currency)
    if cur is None:
        return amount
    img = ""
    path = f"img/currency/{cur.code}.svg"
    if finders.find(path):
        img = static(path)
    return format_html(
        '<span class="money" data-cur="{}"><span class="cur" data-fallback="{}" data-img="{}" '
        'title="{}">{}</span><span class="amt">{}</span></span>',
        cur.code,
        cur.symbol_fallback or cur.code,
        img,
        cur.name,
        cur.symbol,
        _number(amount, cur),
    )


@register.filter
def money_text(amount, currency):
    """Plain text version (emails, PDFs) using the ISO code."""
    cur = _currency(currency)
    return f"{cur.code} {_number(amount, cur)}" if cur else str(amount)


@register.filter
def symbol(currency):
    cur = _currency(currency)
    return cur.symbol if cur else ""


@register.filter
def get_item(mapping, key):
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.filter
def pct(value):
    return f"{Decimal(value):.2f}%" if value is not None else "—"
