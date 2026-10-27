"""A tiny overridable clock. Business code calls ``clock.today()`` instead of ``date.today()`` so that back-dated work
(the demo-data loader, imports, month-end jobs) can run "as of" another date::

    with clock.posting_date(date(2025, 3, 1)):
        issue_policy(...)          # numbers, documents and journals are dated 1 Mar 2025
"""
from contextlib import contextmanager
from datetime import date

_override = {"date": None}


def today():
    return _override["date"] or date.today()


@contextmanager
def posting_date(d):
    old = _override["date"]
    _override["date"] = d
    try:
        yield
    finally:
        _override["date"] = old
