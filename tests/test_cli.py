"""End-to-end CLI tests against the miniature Millennium Dawn tree (offline)."""
from __future__ import annotations

import json
import re

import pytest
from fakes import FakeTransport, completion

from mdde import openrouter
from mdde.__main__ import main
from mdde.cache import TranslationCache, UnchangedLog

KEY = "sk-or-v1-cli-test-key"


@pytest.fixture
def env(md_root, tmp_path, monkeypatch):
    mod = tmp_path / "mod"
    (mod / "tools").mkdir(parents=True)
    (mod / "tools" / "glossary.json").write_text(
        json.dumps({"National Focus": "Nationaler Schwerpunkt"}), encoding="utf-8"
    )
    monkeypatch.setenv("MDDE_MD_ROOT", str(md_root))
    monkeypatch.setenv("MDDE_MOD_ROOT", str(mod))
    monkeypatch.setenv("MDDE_KEY_FILE", str(tmp_path / "no.env"))
    monkeypatch.setenv("MDDE_MODELS", "m1")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    return mod


def number_after(label, text):
    m = re.search(rf"{label}\D*([\d.]+)", text)
    assert m, f"{label!r} fehlt in:\n{text}"
    return int(m.group(1).replace(".", ""))


@pytest.mark.parametrize("scope_name,expected", [("germany", 12), ("full", 19)])
def test_status_counts_open_texts(env, capsys, scope_name, expected):
    assert main(["status", "--scope", scope_name, "--no-quota"]) == 0
    out = capsys.readouterr().out
    assert number_after("Offene Texte", out) == expected
    assert number_after("noch zu", out) == expected


def test_status_subtracts_cached_translations(env, capsys):
    TranslationCache(env / "cache" / "translations.jsonl").put(
        "GER_focus_unity", "German Unity", "Deutsche Einheit", "m"
    )
    assert main(["status", "--no-quota"]) == 0
    out = capsys.readouterr().out
    assert number_after("im Cache", out) == 1
    assert number_after("noch zu", out) == 11


def fake_translator():
    def respond(payload):
        batch = json.loads(payload["messages"][1]["content"])
        return completion({i: f"Übersetzung {i}" for i in batch})

    return FakeTransport(by_model={"m1": respond})


def quota_aware(transport, remaining=900):
    def route(method, url, headers, body, timeout):
        if method == "GET":
            return 200, json.dumps({"data": {"free_model_daily_requests": {"remaining": remaining}}}).encode()
        return transport(method, url, headers, body, timeout)

    return route


def test_translate_fills_cache_and_sends_glossary(env, monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    transport = fake_translator()
    monkeypatch.setattr(openrouter, "urllib_transport", quota_aware(transport))
    assert main(["translate", "--max-requests", "5"]) == 0
    cache = TranslationCache(env / "cache" / "translations.jsonl")
    assert len(cache) == 12
    assert cache.get("GER_focus_unity", "German Unity").startswith("Übersetzung")
    system = transport.calls[0].body["messages"][0]["content"]
    assert "National Focus = Nationaler Schwerpunkt" in system
    out = capsys.readouterr().out
    assert KEY not in out
    assert KEY not in (env / "logs" / "translate.log").read_text()


def test_translate_without_key_fails_cleanly(env, capsys):
    assert main(["translate"]) == 1
    err = capsys.readouterr().err
    assert "OPENROUTER_API_KEY" in err


def test_status_shows_quota_when_key_available(env, monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.setattr(openrouter, "urllib_transport", quota_aware(fake_translator(), remaining=742))
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert number_after("Tageskontingent", out) == 742
    assert KEY not in out


def test_status_counts_unchanged_texts_separately(env, capsys):
    UnchangedLog(env / "cache" / "unchanged.jsonl").add("GER_focus_unity", "German Unity", "m")
    assert main(["status", "--no-quota"]) == 0
    out = capsys.readouterr().out
    assert number_after("unveraendert", out) == 1
    assert number_after("noch zu", out) == 11


def test_translate_skips_unchanged_unless_retry_unchanged(env, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    UnchangedLog(env / "cache" / "unchanged.jsonl").add("GER_focus_unity", "German Unity", "m")
    transport = fake_translator()
    monkeypatch.setattr(openrouter, "urllib_transport", quota_aware(transport))
    assert main(["translate"]) == 0
    sent = [en for c in transport.calls for en in json.loads(c.body["messages"][1]["content"]).values()]
    assert "German Unity" not in sent
    transport2 = fake_translator()
    monkeypatch.setattr(openrouter, "urllib_transport", quota_aware(transport2))
    assert main(["translate", "--retry-unchanged"]) == 0
    sent = [en for c in transport2.calls for en in json.loads(c.body["messages"][1]["content"]).values()]
    assert sent == ["German Unity"]


def test_translate_with_unknown_quota_fails_unless_allowed(env, monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    transport = fake_translator()

    def route(method, url, headers, body, timeout):
        return (500, b"{}") if method == "GET" else transport(method, url, headers, body, timeout)

    monkeypatch.setattr(openrouter, "urllib_transport", route)
    assert main(["translate"]) == 1
    assert transport.calls == []
    assert "Tageskontingent unbekannt" in capsys.readouterr().out
    assert main(["translate", "--allow-unknown-quota"]) == 0
    assert transport.calls


def test_translate_ctrl_c_exits_130(env, monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.setattr(openrouter, "urllib_transport", quota_aware(fake_translator()))

    def interrupted(self, units):
        raise KeyboardInterrupt

    monkeypatch.setattr("mdde.cli.Runner.run", interrupted)
    assert main(["translate"]) == 130
    assert "Abgebrochen" in capsys.readouterr().err


def test_translate_with_broken_key_fails_without_echo(env, monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-SECRETSECRET\nOTHER=1")
    assert main(["translate"]) == 1
    captured = capsys.readouterr()
    assert "ungueltig" in captured.err
    assert "SECRET" not in captured.err + captured.out
