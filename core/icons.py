"""Original inline SVG icons for the interactive vehicle-type and usage pickers (stroke style, currentColor)."""
from django.utils.html import format_html
from django.utils.safestring import mark_safe

_WHEELS = '<circle cx="17" cy="25" r="4"/><circle cx="47" cy="25" r="4"/>'
_BASE = '<path d="M6 25h7M21 25h22M51 25h7"/>'

VEHICLE_SVG = {
    "SALOON": (
        '<path d="M6 25v-5q0-2 2-2.500l10-2.500q3-4 6-5.500h13q4 1.500 7 5.500l9 2q3 .5 3 3v7"/>'
        '<path d="M23 13l-2.500 5H31v-5zM33 13v5h11l-4-5z"/>' + _BASE + _WHEELS
    ),
    "SUV": (
        '<path d="M5 25v-8q0-2 2-2.500l7-1.500 3-6h26q3 0 5 3l4 4.500 6 1.500q2 .5 2 2.500v6.500"/>'
        '<path d="M21 9.500l-2.500 5.500H31V9.500zM33 9.500V15h13l-3.500-5.500zM22 6.500h22"/>' + _BASE + _WHEELS
    ),
    "PICKUP": (
        '<path d="M5 25v-7q0-2 2-2.500l6-1.500 4-5.500h10l4.500 7M38 17.500h21V25M38 21h21"/>'
        '<path d="M20 11.500l-2 4.500h12l-3-4.500z"/>' + _BASE + _WHEELS
    ),
    "VAN": (
        '<path d="M4 25V10q0-2 2-2h34q3 0 5 3l8 6q2 1.500 2 3.500V25"/>'
        '<path d="M8 12h14v6H8zM27 12h10v6H27zM44 11.500l6 4.500h-6z"/>' + _BASE + _WHEELS
    ),
    "BUS": (
        '<path d="M3 25V9q0-2 2-2h50q4 0 5 4v14"/>'
        '<path d="M7 11h8v6H7zM18 11h8v6h-8zM29 11h8v6h-8zM40 11h8v6h-8zM51 11h6v6h-6z"/>'
        '<path d="M5 25h7M20 25h24M52 25h7"/><circle cx="16" cy="25" r="4"/><circle cx="48" cy="25" r="4"/>'
    ),
    "TRUCK": (
        '<path d="M3 21V7h32v14M35 21V11h12l7 6v4"/><path d="M39 13h6l3 4h-9z"/>'
        '<path d="M3 25h5M16 25h4M28 25h14M52 25h6"/><circle cx="12" cy="25" r="4"/><circle cx="24" cy="25" r="4"/><circle cx="47" cy="25" r="4"/>'
    ),
    "MOTORCYCLE": (
        '<circle cx="14" cy="22" r="7"/><circle cx="50" cy="22" r="7"/>'
        '<path d="M14 22l9-10h8l9 10M23 12h-5M46 10l4 12M43 10h6M29 18h9"/>'
    ),
    "TRAILER": (
        '<path d="M10 19h46M3 23l7-4M16 19v-8h16v8M35 19v-6h14v6"/>'
        '<circle cx="38" cy="24" r="4"/><circle cx="49" cy="24" r="4"/>'
    ),
}

USAGE_SVG = {
    "PRIVATE": '<circle cx="16" cy="11" r="5"/><path d="M6 27c0-5 4-8 10-8s10 3 10 8"/>',
    "COMMERCIAL": '<rect x="4" y="11" width="24" height="15" rx="2"/><path d="M12 11V8h8v3M4 18h24"/>',
    "TAXI": '<path d="M12 5h8v4h-8zM5 22v-5l3-6h16l3 6v5zM5 22h22"/><circle cx="10" cy="24" r="2"/><circle cx="22" cy="24" r="2"/>',
    "RENTAL": '<circle cx="10" cy="16" r="5"/><path d="M15 16h13M24 16v4M28 16v3"/>',
    "DRIVING_SCHOOL": '<circle cx="16" cy="16" r="10"/><circle cx="16" cy="16" r="2.500"/><path d="M6 16h7M19 16h7M16 19v7"/>',
    "GOVERNMENT": '<path d="M16 5l11 6H5zM8 13v9M14 13v9M18 13v9M24 13v9M5 24h22"/>',
    "AGRICULTURAL": '<circle cx="10" cy="21" r="6"/><circle cx="26" cy="23" r="3"/><path d="M16 21h7v-8h-6M17 13V8h4v5"/>',
}


def icon_svg(name, size=None):
    if name in VEHICLE_SVG:
        view, body = "0 0 64 32", VEHICLE_SVG[name]
    elif name in USAGE_SVG:
        view, body = "0 0 32 32", USAGE_SVG[name]
    else:
        return ""
    return format_html(
        '<svg class="ico" viewBox="{}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
        'stroke-linejoin="round" aria-hidden="true">{}</svg>',
        view, mark_safe(body),
    )
