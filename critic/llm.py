"""Minimal client for any OpenAI-compatible chat API (OpenRouter, Google AI Studio, Mistral, ...).

Uses only the Python standard library. Retries on rate limits, server errors and timeouts (with
separate retry budgets), waits as long as the provider asks (Retry-After header, or Google's
"retryDelay"), and keeps any reasoning trace apart from the final answer.
"""
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
import zlib
from collections import deque

# Reasoning that some models write inline: <think>, <thinking>, <thought> or <reasoning> blocks.
# An opening tag without a closing one (answer cut off) makes the rest of the text reasoning.
THINK_TAG = re.compile(r"<(think|thinking|thought|reasoning)>(.*?)(?:</\1>|\Z)", flags=re.S | re.I)


def split_reply(content, message=None):
    """Return (final answer text, reasoning text) from a chat message.

    Reasoning can arrive in three ways, and none of it is kept in the answer:
    - a separate field (OpenRouter: message["reasoning"] or message["reasoning_details"]);
    - content chunks of type "thinking" next to chunks of type "text" (Mistral with reasoning on);
    - <think>, <thinking>, <thought> or <reasoning> blocks inside the text (some open models, e.g. Gemma).
    """
    reasoning = []
    if isinstance(content, list):
        texts = []
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text":
                texts.append(c.get("text", ""))
            elif c.get("type") == "thinking":
                inner = c.get("thinking")
                if isinstance(inner, list):
                    reasoning.append("".join(x.get("text", "") for x in inner if isinstance(x, dict)))
                elif isinstance(inner, str):
                    reasoning.append(inner)
        content = "".join(texts)
    content = content or ""
    reasoning += [m[1].strip() for m in THINK_TAG.findall(content)]
    content = THINK_TAG.sub("", content).strip()
    if message:
        if isinstance(message.get("reasoning"), str):
            reasoning.insert(0, message["reasoning"])
        elif isinstance(message.get("reasoning_details"), list):
            reasoning.insert(0, "".join(d.get("text") or d.get("summary") or ""
                                        for d in message["reasoning_details"] if isinstance(d, dict)))
    reasoning = "\n\n".join(r for r in reasoning if r and r.strip())
    return content, (reasoning or None)


def _asked_wait(err, body):
    """Seconds the provider asked us to wait, if it said so."""
    if err is not None and err.headers.get("Retry-After"):
        try:
            return float(err.headers["Retry-After"])
        except ValueError:
            pass
    m = re.search(r'"retryDelay"\s*:\s*"([\d.]+)s"', body or "")
    return float(m.group(1)) if m else None


def _is_timeout(e):
    return isinstance(e, TimeoutError) or (isinstance(e, urllib.error.URLError) and isinstance(e.reason, TimeoutError))


class ChatClient:
    def __init__(self, base_url, api_key_env, timeout=120, max_retries=6, max_timeout_retries=2,
                 initial_backoff=2.0, max_backoff=90.0):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = os.environ.get(api_key_env)
        if not self.api_key:
            raise RuntimeError(f"Environment variable {api_key_env} is not set (see .env.example).")
        self.timeout = timeout
        self.max_retries = max_retries                  # for 429, 5xx and connection errors
        self.max_timeout_retries = max_timeout_retries  # for calls that exceed `timeout`, counted separately
        self.initial_backoff = initial_backoff
        self.max_backoff = max_backoff

    def _backoff(self, attempt, asked=None):
        wait = asked if asked is not None else self.initial_backoff * 2 ** attempt
        return min(self.max_backoff, wait) + random.random()

    def complete(self, model, messages, temperature=None, max_tokens=600, extra=None, timeout=None,
                 before_attempt=None):
        """Return (answer text, info). `info` holds the provider's token usage plus `finish_reason`,
        `reasoning` (trace kept apart from the answer) and `served_by` (OpenRouter's serving provider).
        `before_attempt` is called before every attempt, retries included (used for rate limiting)."""
        body = {"model": model, "messages": messages, "max_tokens": max_tokens, **(extra or {})}
        if temperature is not None:
            body["temperature"] = temperature
        data = json.dumps(body).encode()
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}
        retries = timeouts = 0
        while True:
            if before_attempt:
                before_attempt()
            req = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                    payload = json.loads(resp.read())
                choice = payload["choices"][0]
                message = choice.get("message") or {}
                answer, reasoning = split_reply(message.get("content"), message)
                info = dict(payload.get("usage") or {})
                info.update(finish_reason=choice.get("finish_reason"), reasoning=reasoning,
                            served_by=payload.get("provider"))
                return answer, info
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")
                if e.code in (429, 500, 502, 503, 504) and retries < self.max_retries - 1:
                    time.sleep(self._backoff(retries, _asked_wait(e, detail)))
                    retries += 1
                    continue
                raise RuntimeError(f"HTTP {e.code}: {detail[:300]}") from e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if _is_timeout(e):
                    if timeouts < self.max_timeout_retries:
                        timeouts += 1
                        time.sleep(self._backoff(timeouts - 1))
                        continue
                    raise RuntimeError(f"timeout: no answer within {timeout or self.timeout}s "
                                       f"({timeouts + 1} attempts)") from e
                if retries < self.max_retries - 1:
                    time.sleep(self._backoff(retries))
                    retries += 1
                    continue
                raise RuntimeError(f"network error: {e}") from e


class RateLimiter:
    """Spaces out request starts so that at most `rpm` begin per minute (shared across threads)."""

    def __init__(self, rpm):
        self.interval = 60.0 / rpm
        self.lock = threading.Lock()
        self.next_time = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_time)
            self.next_time = start + self.interval
        time.sleep(max(0.0, start - now))


class TokenLimiter:
    """Keeps the tokens used (input + output) in any rolling `window` seconds at or below `tpm`.

    A call reserves an estimate before it starts (so parallel calls cannot all start at once) and
    replaces it with the actual usage when it ends. Failed calls release their reservation.
    """

    def __init__(self, tpm, window=60.0):
        self.tpm, self.window = tpm, window
        self.lock = threading.Lock()
        self.entries = deque()  # [start time, tokens]

    def reserve(self, estimate):
        while True:
            with self.lock:
                now = time.monotonic()
                while self.entries and now - self.entries[0][0] >= self.window:
                    self.entries.popleft()
                used = sum(e[1] for e in self.entries)
                if used + estimate <= self.tpm or not self.entries:
                    entry = [now, estimate]
                    self.entries.append(entry)
                    return entry
                wait = self.window - (now - self.entries[0][0])
            time.sleep(max(0.05, wait))

    def settle(self, entry, actual):
        with self.lock:
            entry[1] = actual


class MockClient:
    """Offline stand-in for testing the pipeline: returns random but well-formed answers.

    Deterministic per (model, prompt), so the two repeats of a trial get the same answer.
    """

    def complete(self, model, messages, temperature=None, max_tokens=600, extra=None, timeout=None,
                 before_attempt=None):
        rng = random.Random(zlib.crc32((model + "\n" + messages[-1]["content"]).encode()))
        info = {"prompt_tokens": 300, "completion_tokens": rng.randint(500, 5000), "finish_reason": "stop",
                "reasoning": None, "served_by": None}
        if "Reply with the letter only." in messages[-1]["content"]:  # authorship pre-test
            return rng.choice("ABCD"), info
        answer = {"issues": [], "passes_rules": rng.random() < 0.5, "score": rng.randint(1, 10)}
        return json.dumps(answer), info


def parse_answer(text):
    """Extract the JSON verdict from a model reply. Returns (dict or None, error message or None)."""
    if not text:
        return None, "empty reply"
    cleaned = THINK_TAG.sub("", text)  # in case a reply still carries thoughts
    match = re.search(r"\{.*\}", cleaned, flags=re.S)
    if not match:
        return None, "no JSON object found"
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e}"
    score, passes = obj.get("score"), obj.get("passes_rules")
    if isinstance(score, str) and score.strip().isdigit():
        score = int(score)
    if not isinstance(score, int) or not 1 <= score <= 10:
        return None, f"bad score: {score!r}"
    if not isinstance(passes, bool):
        return None, f"bad passes_rules: {passes!r}"
    issues = obj.get("issues", [])
    return {"score": score, "passes_rules": passes, "issues": issues if isinstance(issues, list) else [str(issues)]}, None
