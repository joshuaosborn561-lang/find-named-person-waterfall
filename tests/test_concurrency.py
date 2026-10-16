"""Vendor pool sizes. SITE_PEOPLE_CONCURRENCY defaults to 6 and caps at 12."""

from people_waterfall.concurrency import (
    DEFAULT_SITE_PEOPLE_CONCURRENCY,
    MAX_SITE_PEOPLE_CONCURRENCY,
    site_people_concurrency,
    vendor_concurrency,
    vendor_gate,
)


def test_site_people_concurrency_default_and_cap(monkeypatch):
    monkeypatch.delenv("SITE_PEOPLE_CONCURRENCY", raising=False)
    assert site_people_concurrency() == DEFAULT_SITE_PEOPLE_CONCURRENCY
    assert site_people_concurrency() == 6
    monkeypatch.setenv("SITE_PEOPLE_CONCURRENCY", "4")
    assert site_people_concurrency() == 4
    monkeypatch.setenv("SITE_PEOPLE_CONCURRENCY", "99")
    assert site_people_concurrency() == MAX_SITE_PEOPLE_CONCURRENCY
    assert site_people_concurrency() == 12
    monkeypatch.setenv("SITE_PEOPLE_CONCURRENCY", "0")
    assert site_people_concurrency() == 1
    monkeypatch.setenv("SITE_PEOPLE_CONCURRENCY", "nope")
    assert site_people_concurrency() == 6
    monkeypatch.setenv("SITE_PEOPLE_CONCURRENCY", " 8 ")
    assert site_people_concurrency() == 8


def test_vendor_concurrency_uses_site_people_cap(monkeypatch):
    vendor_gate.reset()
    monkeypatch.setenv("SITE_PEOPLE_CONCURRENCY", "12")
    assert vendor_concurrency("site_people") == 12
    monkeypatch.setenv("SITE_PEOPLE_CONCURRENCY", "100")
    assert vendor_concurrency("site_people") == 12
