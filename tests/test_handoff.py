from people_waterfall.handoff import _mcp_url, handoff_title_matches
from people_waterfall.profile import parse_profile


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


def test_mcp_url_appends_mcp():
    assert _mcp_url("https://email.example") == "https://email.example/mcp"
    assert _mcp_url("https://email.example/mcp/") == "https://email.example/mcp"


def test_handoff_calls_ensure_client_first(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_call(url, name, arguments, timeout=45):
        calls.append((name, arguments))
        return _Resp(200)

    monkeypatch.setattr("people_waterfall.handoff._mcp_call", fake_call)
    monkeypatch.setattr(
        "people_waterfall.handoff.settings",
        type("S", (), {"email_waterfall_url": "https://email.example"})(),
    )
    profile = parse_profile("goliath", {"target_titles": ["Owner"]})
    result = handoff_title_matches(profile)
    assert [name for name, _ in calls] == ["ensure_client", "enrich_waterfall"]
    assert calls[0][1]["client_tag"] == "goliath"
    assert calls[1][1]["source_table"] == "public.goliath_wf_contacts"
    assert calls[1][1]["client_tag"] == "goliath"
    assert calls[1][1]["max_tier"] == "aiark"
    assert result["queued"] is True
    assert result["ensured"] is True


def test_handoff_stops_when_ensure_client_404s(monkeypatch):
    calls: list[str] = []

    def fake_call(url, name, arguments, timeout=45):
        calls.append(name)
        return _Resp(404 if name == "ensure_client" else 200)

    monkeypatch.setattr("people_waterfall.handoff._mcp_call", fake_call)
    monkeypatch.setattr(
        "people_waterfall.handoff.settings",
        type("S", (), {"email_waterfall_url": "https://email.example/mcp"})(),
    )
    profile = parse_profile("goliath", {"target_titles": ["Owner"]})
    result = handoff_title_matches(profile)
    assert calls == ["ensure_client"]
    assert result["queued"] is False
    assert result["http_status"] == 404
