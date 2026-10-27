from django import template

from core.icons import icon_svg

register = template.Library()


@register.simple_tag
def icon(name):
    return icon_svg(name)
