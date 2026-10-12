"""OpenRouter client: request shape, model chain and error handling (offline)."""
from __future__ import annotations

import json

import pytest
from fakes import FakeSleep, FakeTransport, completion, error

from mdde.openrouter import (
    AuthError,
    Client,
    KeyNotFound,
    NoModelAvailable,
    QuotaExhausted,
    extract_json_object,
    load_api_key,
)

KEY = "sk-or-v1-0123456789abcdef-test-only"
M1, M2 = "m1/model:free", "m2/model:free"
BATCH = {"0": "German Unity", "1": "§YWar§! is coming"}
GOOD = {"0": "Deutsche Einheit", "1": "§YKrieg§! kommt"}


def make_client(transport, sleep=None, models=(M1, M2), **kw):
    return Client(KEY, list(models), transport=transport, sleep=sleep or FakeSleep(),
                  system_prompt="SYSTEM", **kw)


def test_request_has_the_documented_shape():
    transport = FakeTransport([completion(GOOD)])
    make_client(transport).translate_batch(BATCH)
    (call,) = transport.calls
    assert call.method == "POST"
    assert call.url == "https://openrouter.ai/api/v1/chat/completions"
    assert call.headers["Authorization"] == f"Bearer {KEY}"
    assert call.headers["Content-Type"] == "application/json"
    assert call.timeout == 300
    assert call.body == {
        "model": M1,
        "messages": [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": json.dumps(BATCH, ensure_ascii=False)},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "reasoning": {"effort": "low", "exclude": True},
    }


def test_success_returns_texts_model_and_usage():
    client = make_client(FakeTransport([completion(GOOD, prompt_tokens=120, completion_tokens=30)]))
    result = client.translate_batch(BATCH)
    assert result.ok
    assert result.texts == GOOD
    assert result.model == M1
    assert client.stats["requests"] == 1
    assert client.stats["tok_in"] == 120
    assert client.stats["tok_out"] == 30


def test_ids_outside_the_batch_are_dropped():
    client = make_client(FakeTransport([completion({**GOOD, "99": "extra"})]))
    assert client.translate_batch(BATCH).texts == GOOD


def test_429_switches_to_next_model_without_waiting():
    sleep = FakeSleep()
    transport = FakeTransport([error(429, "rate-limited upstream"), completion(GOOD)])
    result = make_client(transport, sleep).translate_batch(BATCH)
    assert [c.model for c in transport.calls] == [M1, M2]
    assert result.model == M2
    assert sleep.calls == []


def test_all_models_429_waits_then_retries_chain():
    sleep = FakeSleep()
    transport = FakeTransport([error(429), error(429), completion(GOOD)])
    result = make_client(transport, sleep).translate_batch(BATCH)
    assert sleep.calls == [60]
    assert [c.model for c in transport.calls] == [M1, M2, M1]
    assert result.ok and result.model == M1


def test_wait_grows_and_is_capped_at_five_minutes():
    sleep = FakeSleep()
    transport = FakeTransport([error(429)] * 12 + [completion(GOOD)])
    make_client(transport, sleep).translate_batch(BATCH)
    assert sleep.calls == [60, 120, 180, 240, 300, 300]


def test_waiting_gives_up_after_max_waits():
    sleep = FakeSleep()
    transport = FakeTransport([error(429)] * 6)
    result = make_client(transport, sleep, max_waits=2).translate_batch(BATCH)
    assert not result.ok
    assert sleep.calls == [60, 120]
    assert len(transport.calls) == 6


@pytest.mark.parametrize("status", [403, 404])
def test_forbidden_or_missing_model_is_dropped_for_the_run(status):
    transport = FakeTransport([error(status), completion(GOOD), completion(GOOD)])
    client = make_client(transport)
    client.translate_batch(BATCH)
    client.translate_batch(BATCH)
    assert [c.model for c in transport.calls] == [M1, M2, M2]
    assert client.disabled == {M1}


def test_all_models_gone_raises():
    client = make_client(FakeTransport([error(404), error(403)]))
    with pytest.raises(NoModelAvailable):
        client.translate_batch(BATCH)


@pytest.mark.parametrize(
    "failure",
    [
        error(500),
        error(502),
        TimeoutError("timed out"),
        OSError("connection reset"),
        completion("ich habe keine Lust"),
        (200, b"<html>gateway</html>"),
        (200, json.dumps({"error": {"message": "provider down", "code": 502}}).encode()),
        (200, json.dumps({"choices": [{"message": {"content": None}}]}).encode()),
    ],
)
def test_transient_failure_moves_to_next_model(failure):
    sleep = FakeSleep()
    transport = FakeTransport([failure, completion(GOOD)])
    result = make_client(transport, sleep).translate_batch(BATCH)
    assert result.ok and result.model == M2
    assert sleep.calls == []


def test_transient_failures_stop_after_three_rounds():
    sleep = FakeSleep()
    transport = FakeTransport([error(500)] * 6 + [completion(GOOD)])
    result = make_client(transport, sleep).translate_batch(BATCH)
    assert not result.ok
    assert len(transport.calls) == 6
    assert sleep.calls == []


def test_daily_free_limit_raises_quota_exhausted():
    msg = "Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000 free model requests per day"
    client = make_client(FakeTransport([error(429, msg)]))
    with pytest.raises(QuotaExhausted):
        client.translate_batch(BATCH)


@pytest.mark.parametrize("status", [401, 402])
def test_auth_or_payment_errors_abort_without_leaking_the_key(status):
    client = make_client(FakeTransport([error(status, f"bad key {KEY}")]))
    with pytest.raises(AuthError) as excinfo:
        client.translate_batch(BATCH)
    assert KEY not in str(excinfo.value)
    assert KEY not in repr(client)
    assert all(KEY not in e for e in client.errors)


def test_error_log_never_contains_the_key():
    client = make_client(FakeTransport([error(500, f"echo {KEY}"), completion(GOOD)]))
    client.translate_batch(BATCH)
    assert client.errors and all(KEY not in e for e in client.errors)


def test_before_request_hook_runs_before_each_request_and_can_stop():
    seen = []

    class Stop(Exception):
        pass

    def hook():
        seen.append(len(transport.calls))
        if len(seen) == 2:
            raise Stop

    transport = FakeTransport([error(429), completion(GOOD)])
    client = make_client(transport, before_request=hook)
    with pytest.raises(Stop):
        client.translate_batch(BATCH)
    assert seen == [0, 1]
    assert len(transport.calls) == 1


def test_quota_reads_free_model_daily_requests():
    payload = {"data": {"label": "sk-or-v1-abc...xyz", "usage": 0,
                        "free_model_daily_requests": {"limit": 1000, "remaining": 742}}}
    transport = FakeTransport([(200, json.dumps(payload).encode())])
    quota = make_client(transport).quota()
    assert quota == {"limit": 1000, "remaining": 742}
    (call,) = transport.calls
    assert call.method == "GET"
    assert call.url == "https://openrouter.ai/api/v1/key"
    assert call.headers["Authorization"] == f"Bearer {KEY}"


@pytest.mark.parametrize(
    "response",
    [
        (200, json.dumps({"data": {"usage": 0}}).encode()),
        (200, b"not json"),
        error(500),
        OSError("down"),
    ],
)
def test_quota_unknown_returns_none(response):
    assert make_client(FakeTransport([response])).quota() is None


def test_quota_request_does_not_count_as_model_request():
    payload = {"data": {"free_model_daily_requests": {"remaining": 5}}}
    client = make_client(FakeTransport([(200, json.dumps(payload).encode())]))
    client.quota()
    assert client.stats["requests"] == 0


@pytest.mark.parametrize(
    "content,expected",
    [
        ('{"0": "a"}', {"0": "a"}),
        ('Sure! Here is the JSON:\n{"0": "a", "1": "b"}\nHope it helps {:)}', {"0": "a", "1": "b"}),
        ('```json\n{"0": "{nested} text"}\n```', {"0": "{nested} text"}),
        ('I think {maybe} this: {"0": "a"}', {"0": "a"}),
        ('{"irrelevant": 1} then {"0": "a"}', {"0": "a"}),
        ("no json at all", None),
        ("", None),
    ],
)
def test_extract_json_object(content, expected):
    assert extract_json_object(content, {"0", "1"}) == expected


def test_load_api_key_prefers_environment(tmp_path):
    keyfile = tmp_path / ".env"
    keyfile.write_text("OPENROUTER_API_KEY=from-file\n")
    assert load_api_key({"OPENROUTER_API_KEY": " from-env "}, keyfile) == "from-env"


@pytest.mark.parametrize(
    "text",
    ['OPENROUTER_API_KEY="sk-or-v1-file"\n', "X=1\nOPENROUTER_KEY = 'sk-or-v1-file'\n"],
)
def test_load_api_key_from_file(tmp_path, text):
    keyfile = tmp_path / ".env"
    keyfile.write_text(text)
    assert load_api_key({}, keyfile) == "sk-or-v1-file"


def test_missing_key_raises_without_content(tmp_path):
    keyfile = tmp_path / ".env"
    keyfile.write_text("OTHER_SECRET=topsecret\n")
    with pytest.raises(KeyNotFound) as excinfo:
        load_api_key({}, keyfile)
    assert "topsecret" not in str(excinfo.value)
    with pytest.raises(KeyNotFound):
        load_api_key({}, tmp_path / "missing.env")


def test_unknown_quota_structure_exposes_field_names_only():
    payload = {"data": {"label": "sk-or-v1-abc...xyz", "usage": 3, "limits": {"daily": 1}}}
    client = make_client(FakeTransport([(200, json.dumps(payload).encode())]))
    assert client.quota() is None
    assert client.quota_fields == ["label", "limits", "usage"]
