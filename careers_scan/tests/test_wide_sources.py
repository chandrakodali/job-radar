from datetime import date

from careers_scan.wide import sources


def test_hn_keeps_top_level_employer_domains(monkeypatch):
    def get(url, params=None):
        if params:
            return {"hits": [{"title": "Ask HN: Who is hiring? (November 2026)", "objectID": "123"},
                             {"title": "Ask HN: Who wants to be hired?", "objectID": "456"}]}
        assert url.endswith("123")
        return {"children": [{"text": '<a href="https://acme.com/jobs">Acme</a> hr@widgets.io '
                                      'me@gmail.com <a href="https://linkedin.com/company/acme">Acme</a>',
                              "children": [{"text": "hr@reply-only.co"}]}]}

    monkeypatch.setattr(sources, "get_json", get)
    assert set(filter(None, sources.hacker_news())) == {
        ("acme.com", "https://acme.com/"), ("widgets.io", "https://widgets.io/")}


def test_phrase_search_requires_key_and_uses_basic_credits(monkeypatch):
    calls = []

    def post(_, payload, headers):
        calls.append(payload)
        assert headers["Authorization"] == "Bearer fake"
        return {"results": [{"url": "https://acme.com/careers"}]}

    monkeypatch.setattr(sources, "post_json", post)
    assert list(sources.phrase_search(None, date(2026, 11, 14))) == []
    assert len(list(sources.phrase_search("fake", date(2026, 11, 14)))) == 30
    assert len({call["query"] for call in calls}) == 30
    assert all(call["search_depth"] == "basic" for call in calls)
