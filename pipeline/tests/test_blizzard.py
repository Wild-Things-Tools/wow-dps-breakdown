"""The Blizzard client, driven by a mock transport instead of the real API.

Credentials are Actions secrets, so a development checkout cannot reach the service.
What can be pinned here is everything that does not need the server to be present:
that the right namespace goes with the right endpoint, that the token is fetched
once and re-fetched after a 401, that a 404 is reported rather than raised, that the
disk cache spares a second request, and that ``id_from_href`` refuses a link
pointing at the wrong kind of thing.

The thing this cannot test is whether the endpoints answer the way the schema says.
That is what the workflow is for.
"""

from __future__ import annotations

import httpx
import pytest

from wowdps.blizzard import (
    BlizzardClient,
    BlizzardError,
    Credentials,
    RequestBudgetExhausted,
    id_from_href,
)

CREDENTIALS = Credentials(client_id="id", client_secret="secret")


class Recorder:
    """A transport that answers from a table and remembers what it was asked."""

    def __init__(self, routes: dict[str, object] | None = None) -> None:
        self.routes = routes or {}
        self.requests: list[httpx.Request] = []
        self.token_requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth.battle.net":
            self.token_requests += 1
            return httpx.Response(200, json={"access_token": f"tok{self.token_requests}"})

        self.requests.append(request)
        answer = self.routes.get(request.url.path, {})
        if isinstance(answer, int):
            return httpx.Response(answer, json={"error": "no"})
        if callable(answer):
            return answer(request)
        return httpx.Response(200, json=answer)

    @property
    def paths(self) -> list[str]:
        return [str(r.url.path) for r in self.requests]

    def params_for(self, path: str) -> dict:
        for request in self.requests:
            if request.url.path == path:
                return dict(request.url.params)
        raise AssertionError(f"{path} was never requested")


def client(recorder: Recorder, **kwargs) -> BlizzardClient:
    return BlizzardClient(
        CREDENTIALS,
        transport=httpx.MockTransport(recorder),
        rate_per_second=0,  # no pacing: the tests must not sleep
        **kwargs,
    )


def test_journal_uses_the_static_namespace_and_keystone_the_dynamic_one():
    """The namespace is per endpoint family, and getting it wrong is a 404."""
    recorder = Recorder(
        {
            "/data/wow/journal-encounter/2500": {"id": 2500},
            "/data/wow/mythic-keystone/season/index": {"current_season": {"id": 15}},
        }
    )
    with client(recorder, region="eu", locale="en_GB") as api:
        api.journal_encounter(2500)
        api.mythic_keystone_season_index()

    assert recorder.params_for("/data/wow/journal-encounter/2500") == {
        "namespace": "static-eu",
        "locale": "en_GB",
    }
    assert (
        recorder.params_for("/data/wow/mythic-keystone/season/index")["namespace"] == "dynamic-eu"
    )


def test_requests_go_to_the_region_host():
    recorder = Recorder({"/data/wow/journal-expansion/index": {"tiers": []}})
    with client(recorder, region="kr") as api:
        api.journal_expansion_index()
    assert recorder.requests[0].url.host == "kr.api.blizzard.com"


def test_the_token_is_fetched_once_for_many_requests():
    recorder = Recorder({"/data/wow/journal-encounter/1": {}, "/data/wow/journal-encounter/2": {}})
    with client(recorder) as api:
        api.journal_encounter(1)
        api.journal_encounter(2)
    assert recorder.token_requests == 1
    assert recorder.requests[0].headers["Authorization"] == "Bearer tok1"


def test_a_401_re_authenticates_once():
    """Tokens last a day; a long walk can outlive one."""
    seen: list[int] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        if len(seen) == 1:
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(200, json={"id": 7})

    recorder = Recorder({"/data/wow/journal-encounter/7": flaky})
    with client(recorder) as api:
        assert api.journal_encounter(7) == {"id": 7}
    assert recorder.token_requests == 2


def test_a_404_is_reported_not_raised():
    """A walk of fifteen hundred encounters must survive one that is not there."""
    recorder = Recorder({"/data/wow/journal-encounter/9999": 404})
    with client(recorder) as api:
        assert api.journal_encounter(9999) is None
        assert [f.status for f in api.ledger.failures] == [404]


def test_other_errors_do_raise():
    recorder = Recorder({"/data/wow/journal-encounter/1": 500})
    with client(recorder) as api:
        with pytest.raises(BlizzardError):
            api.journal_encounter(1)


def test_a_429_is_retried_with_the_servers_own_wait(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("wowdps.blizzard.time.sleep", slept.append)

    calls: list[int] = []

    def limited(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={})
        return httpx.Response(200, json={"id": 1})

    recorder = Recorder({"/data/wow/journal-encounter/1": limited})
    with client(recorder) as api:
        assert api.journal_encounter(1) == {"id": 1}
        assert api.ledger.retries == 1
        # Both attempts count against the budget: the server saw them both.
        assert api.ledger.requests == 2
    assert slept == [2.0]


def test_the_cache_spares_the_second_request(tmp_path):
    recorder = Recorder({"/data/wow/journal-encounter/1": {"id": 1}})
    with client(recorder, cache_dir=tmp_path) as api:
        first = api.journal_encounter(1)
        second = api.journal_encounter(1)

    assert first == second == {"id": 1}
    assert recorder.paths == ["/data/wow/journal-encounter/1"]
    assert api.ledger.requests == 1 and api.ledger.cache_hits == 1


def test_the_cache_key_separates_locales(tmp_path):
    """Names are the join key, so two locales must not share one cached answer."""
    recorder = Recorder({"/data/wow/journal-encounter/1": {"id": 1}})
    with client(recorder, cache_dir=tmp_path, locale="en_US") as api:
        api.journal_encounter(1)
    with client(recorder, cache_dir=tmp_path, locale="de_DE") as api:
        api.journal_encounter(1)
    assert len(recorder.paths) == 2


def test_max_requests_stops_the_walk():
    recorder = Recorder({f"/data/wow/journal-encounter/{i}": {"id": i} for i in range(5)})
    with client(recorder, max_requests=2) as api:
        api.journal_encounter(0)
        api.journal_encounter(1)
        with pytest.raises(RequestBudgetExhausted):
            api.journal_encounter(2)
    assert api.ledger.requests == 2


def test_the_ledger_counts_by_endpoint():
    recorder = Recorder(
        {
            "/data/wow/journal-encounter/1": {},
            "/data/wow/journal-instance/2": {},
        }
    )
    with client(recorder) as api:
        api.journal_encounter(1)
        api.journal_instance(2)
    assert api.ledger.to_json()["byEndpoint"] == {"journal-encounter": 1, "journal-instance": 1}


def test_connected_realm_index_reads_ids_out_of_hrefs():
    recorder = Recorder(
        {
            "/data/wow/connected-realm/index": {
                "connected_realms": [
                    {"href": "https://us.api.blizzard.com/data/wow/connected-realm/121?ns=x"},
                    {"href": "https://us.api.blizzard.com/data/wow/connected-realm/11?ns=x"},
                ]
            }
        }
    )
    with client(recorder) as api:
        # Sorted, so the pass picks the same realm every time and the cache stays warm.
        assert api.connected_realm_index() == [11, 121]


@pytest.mark.parametrize(
    ("href", "expect", "wanted"),
    [
        ("https://us.api.blizzard.com/data/wow/journal-instance/1303?namespace=x", 1303, None),
        ("https://us.api.blizzard.com/data/wow/journal-instance/1303", 1303, "journal-instance"),
        ("https://us.api.blizzard.com/data/wow/item/270160?namespace=x", 270160, "item"),
        (None, None, None),
        ("https://example.invalid/nothing-numeric", None, None),
    ],
)
def test_id_from_href(href, expect, wanted):
    assert id_from_href(href, wanted) == expect


def test_id_from_href_refuses_the_wrong_kind_of_link():
    """The keystone dungeon's link is the only thing that says it is an instance id."""
    href = "https://us.api.blizzard.com/data/wow/mythic-keystone/dungeon/542?namespace=x"
    assert id_from_href(href, "journal-instance") is None
    assert id_from_href(href) == 542


def test_an_unknown_region_is_refused_up_front():
    with pytest.raises(BlizzardError):
        BlizzardClient(CREDENTIALS, region="xx")


def test_credentials_say_where_to_get_them(monkeypatch):
    monkeypatch.delenv("BLIZZARD_CLIENT_ID", raising=False)
    monkeypatch.delenv("BLIZZARD_CLIENT_SECRET", raising=False)
    with pytest.raises(BlizzardError, match="develop.battle.net"):
        Credentials.from_env()
