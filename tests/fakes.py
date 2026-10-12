"""Test doubles for the OpenRouter HTTP transport."""
from __future__ import annotations

import json
from dataclasses import dataclass, field


def completion(obj: object, prefix: str = "", suffix: str = "", prompt_tokens: int = 100,
               completion_tokens: int = 50) -> tuple[int, bytes]:
    """A complete chat-completions response as OpenRouter returns it."""
    content = prefix + (obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)) + suffix
    body = {
        "id": "gen-1",
        "provider": "Test",
        "model": "x",
        "object": "chat.completion",
        "created": 1,
        "choices": [
            {
                "logprobs": None,
                "finish_reason": "stop",
                "native_finish_reason": "stop",
                "index": 0,
                "message": {"role": "assistant", "content": content, "refusal": None, "reasoning": None},
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    return 200, json.dumps(body).encode()


def error(status: int, message: str = "error") -> tuple[int, bytes]:
    return status, json.dumps({"error": {"message": message, "code": status}}).encode()


@dataclass
class Call:
    method: str
    url: str
    headers: dict
    body: dict | None
    timeout: float

    @property
    def model(self) -> str | None:
        return self.body.get("model") if self.body else None


@dataclass
class FakeTransport:
    """Returns scripted responses in order; an Exception instance is raised instead.

    ``by_model`` maps a model name to a responder used when the script is empty,
    which keeps concurrency tests independent of call order.
    """

    script: list = field(default_factory=list)
    by_model: dict = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def __call__(self, method, url, headers, body, timeout):
        payload = json.loads(body) if body else None
        call = Call(method, url, dict(headers), payload, timeout)
        self.calls.append(call)
        if self.script:
            response = self.script.pop(0)
        else:
            response = self.by_model[call.model](payload)
        if isinstance(response, Exception):
            raise response
        return response


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
