from decimal import Decimal

from django import template
from django.utils.html import format_html

from core.templatetags.money import money

register = template.Library()


@register.filter
def fmt(value, metric):
    """Format a budget figure: money for amounts, integer for counts, percent for ratios."""
    if value is None:
        return format_html('<span class="muted">—</span>')
    if metric == "POLICIES":
        return f"{Decimal(value):,.0f}"
    if metric in ("LOSS", "EXPENSE", "COMBINED"):
        return f"{Decimal(value):,.1f}%"
    return money(value, "OMR")


@register.filter
def signed(value, metric):
    if value is None:
        return format_html('<span class="muted">—</span>')
    s = fmt(abs(value), metric)
    return format_html("{}{}", "+" if value > 0 else "−" if value < 0 else "", s)


@register.filter
def pct1(value):
    return "—" if value is None else f"{value:+.1f}%"


@register.filter
def favcls(fav):
    return {True: "fav", False: "adv"}.get(fav, "")


@register.filter
def idx(seq, i):
    try:
        return seq[i]
    except (IndexError, TypeError, KeyError):
        return ""


@register.filter
def getk(d, key):
    return d.get(key) if hasattr(d, "get") else ""


@register.filter
def get_count(d, key):
    return d.get(key, 0)
