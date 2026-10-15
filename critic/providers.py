"""Model list and routing to API providers (OpenRouter, Google AI Studio, Mistral, or any
OpenAI-compatible endpoint), with rate limits, timeouts, reasoning settings and a pause when a
model's quota runs out. All settings live in config.yaml (see the comments there).
"""
import json
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from critic.llm import ChatClient, MockClient, RateLimiter, TokenLimiter

DEFAULT_PROVIDERS = {"openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY"}}
PAUSE_AFTER = 8  # consecutive rate-limit failures (after retries) before a model is paused for this run
REASONING_MODES = ("default", "on", "off")
MODEL_KEYS = {"name", "provider", "model", "temperature", "max_tokens", "timeout", "rpm", "tpm", "concurrency",
              "reasoning", "reasoning_effort", "openrouter_provider", "extra", "price_in", "price_out", "max_cost"}


class ModelPaused(Exception):
    """Raised for calls to a model whose quota seems exhausted; its remaining trials wait for a re-run."""


class BudgetReached(ModelPaused):
    """Raised for calls after the spending cap is reached; the remaining trials wait, unrecorded."""


def token_counts(usage):
    """(input, output, reasoning) tokens from a provider's usage block; None where not reported."""
    usage = usage or {}
    details = usage.get("completion_tokens_details") or {}
    return usage.get("prompt_tokens"), usage.get("completion_tokens"), details.get("reasoning_tokens")


def provider_kind(p):
    """Which API dialect a provider speaks: set `kind` in config.yaml, or it is guessed from the URL."""
    if p.get("kind"):
        return p["kind"]
    url = p["base_url"]
    for host, kind in (("openrouter.ai", "openrouter"), ("generativelanguage.googleapis.com", "google"),
                       ("api.mistral.ai", "mistral")):
        if host in url:
            return kind
    return "openai-compatible"


def reasoning_fields(kind, mode, effort):
    """Request fields for the reasoning setting. "default" sends nothing: the provider decides, which
    for several models (e.g. gpt-oss, DeepSeek) means reasoning ON. "off" must be sent explicitly."""
    if mode in (None, "default"):
        return {}
    if kind == "openrouter":
        if mode == "off":
            return {"reasoning": {"enabled": False}}
        r = {"enabled": True, "exclude": False}  # exclude: False -> the trace comes back and is stored
        if effort:
            r["effort"] = effort
        return {"reasoning": r}
    # Google AI Studio, Mistral and other OpenAI-style APIs take a single reasoning_effort field.
    return {"reasoning_effort": "none" if mode == "off" else (effort or "medium")}


@dataclass
class ModelSpec:
    name: str
    provider: str
    model: str
    settings: dict = field(default_factory=dict)  # temperature / max_tokens / timeout, if set per model
    request_extra: dict = field(default_factory=dict)  # reasoning, routing and `extra` fields, merged
    rpm: float = None
    tpm: float = None
    concurrency: int = None
    reasoning: str = "default"
    price_in: float = None    # USD per 1M input tokens (only needed where the provider does not report cost)
    price_out: float = None   # USD per 1M output tokens (reasoning tokens are billed as output)
    max_cost: float = None    # spending cap for this model, in USD

    def cost(self, usage):
        """Cost of one call: the provider's own figure if it reports one (OpenRouter does),
        else tokens x the prices in config.yaml, else None (unknown)."""
        usage = usage or {}
        if isinstance(usage.get("cost"), (int, float)):
            return float(usage["cost"])
        if self.price_in is None and self.price_out is None:
            return None
        tin, tout, _ = token_counts(usage)
        return ((tin or 0) * (self.price_in or 0) + (tout or 0) * (self.price_out or 0)) / 1e6

    def call_args(self, exp):
        temperature = self.settings["temperature"] if "temperature" in self.settings else exp.get("temperature")
        return (temperature, self.settings.get("max_tokens", exp["max_tokens"]),
                self.request_extra or None, self.settings.get("timeout", exp.get("timeout")))


def load_providers(cfg):
    if cfg.get("providers"):
        return cfg["providers"]
    if cfg.get("api"):  # older config format: one OpenAI-compatible API
        return {"openrouter": cfg["api"]}
    return DEFAULT_PROVIDERS


def load_models(cfg):
    providers = load_providers(cfg)
    default = next(iter(providers))
    specs = []
    for entry in cfg["models"]:
        if isinstance(entry, str):
            entry = {"model": entry}
        unknown = set(entry) - MODEL_KEYS
        if unknown:
            raise ValueError(f"model {entry.get('name', entry.get('model'))}: unknown setting(s) {sorted(unknown)}")
        provider = entry.get("provider", default)
        if provider not in providers:
            raise ValueError(f"model {entry} uses provider '{provider}', which is not under `providers`")
        kind = provider_kind(providers[provider])
        name = str(entry.get("name", entry["model"]))
        mode = entry.get("reasoning", "default")
        if isinstance(mode, bool):  # YAML reads unquoted on/off as true/false
            mode = "on" if mode else "off"
        if mode not in REASONING_MODES:
            raise ValueError(f"model {name}: reasoning must be one of {REASONING_MODES}, not {mode!r}")
        if entry.get("reasoning_effort") and mode != "on":
            raise ValueError(f"model {name}: reasoning_effort needs `reasoning: on`")
        request_extra = reasoning_fields(kind, mode, entry.get("reasoning_effort"))
        if entry.get("openrouter_provider"):
            if kind != "openrouter":
                raise ValueError(f"model {name}: openrouter_provider only works with an OpenRouter provider")
            request_extra["provider"] = dict(entry["openrouter_provider"])
        for k, v in (entry.get("extra") or {}).items():
            if k in request_extra:
                raise ValueError(f"model {name}: '{k}' is set both in `extra` and by another setting; keep one")
            request_extra[k] = v
        specs.append(ModelSpec(
            name=name, provider=provider, model=entry["model"],
            settings={k: entry[k] for k in ("temperature", "max_tokens", "timeout") if k in entry},
            request_extra=request_extra, rpm=entry.get("rpm"), tpm=entry.get("tpm"),
            concurrency=entry.get("concurrency"), reasoning=mode, price_in=entry.get("price_in"),
            price_out=entry.get("price_out"), max_cost=entry.get("max_cost")))
    names = [s.name for s in specs]
    if len(set(names)) != len(names):
        raise ValueError(f"model names must be unique: {names}")
    return specs


class Router:
    """Sends each call to the right provider. Requests-per-minute limits apply to every attempt
    (retries included); tokens-per-minute limits reserve an estimate before a call and count the
    actual usage after it. A model is paused after PAUSE_AFTER consecutive rate-limit failures."""

    def __init__(self, cfg, specs, dry_run=False, spent=None):
        exp = cfg["experiment"]
        # Spending caps: `budget.max_cost` for the whole run, `max_cost` per model. `spent` carries
        # what earlier (resumed) runs already cost, so a cap holds across re-runs.
        self.max_cost = (cfg.get("budget") or {}).get("max_cost")
        self.spent = defaultdict(float, spent or {})
        self.over_budget = set()
        self.providers = load_providers(cfg)
        self.dry_run = dry_run
        self.clients, self.rpm, self.tpm = {}, {}, {}
        for s in specs:  # create every client now, so a missing API key fails before any call
            p = self.providers[s.provider]
            if s.provider not in self.clients:
                self.clients[s.provider] = MockClient() if dry_run else ChatClient(
                    p["base_url"], p["api_key_env"], timeout=exp.get("timeout", 120),
                    max_retries=exp.get("max_retries", 6), max_timeout_retries=exp.get("max_timeout_retries", 2),
                    initial_backoff=exp.get("initial_backoff", 2.0), max_backoff=exp.get("max_backoff", 90.0))
            if dry_run:
                continue
            if p.get("rpm"):
                self.rpm.setdefault(("provider", s.provider), RateLimiter(p["rpm"]))
            if p.get("tpm"):
                self.tpm.setdefault(("provider", s.provider), TokenLimiter(p["tpm"]))
            if s.rpm:
                self.rpm[("model", s.name)] = RateLimiter(s.rpm)
            if s.tpm:
                self.tpm[("model", s.name)] = TokenLimiter(s.tpm)
        self.lock = threading.Lock()
        self.streak = {s.name: 0 for s in specs}
        self.token_estimate = {}
        self.paused = set()

    def _keys(self, spec):
        return (("provider", spec.provider), ("model", spec.name))

    def total_spent(self):
        return sum(self.spent.values())

    def _check_budget(self, spec):
        with self.lock:
            if self.max_cost is not None and self.total_spent() >= self.max_cost:
                key, why = "*", f"run budget reached (${self.total_spent():.2f} of ${self.max_cost:.2f})"
            elif spec.max_cost is not None and self.spent[spec.name] >= spec.max_cost:
                key, why = spec.name, f"{spec.name}: budget reached (${self.spent[spec.name]:.2f} of ${spec.max_cost:.2f})"
            else:
                return
            if key not in self.over_budget:
                self.over_budget.add(key)
                print(f"  !! {why}. No new calls; re-run with a higher cap to continue.")
        raise BudgetReached(spec.name)

    def complete(self, spec, messages, exp):
        if spec.name in self.paused:
            raise ModelPaused(spec.name)
        self._check_budget(spec)
        temperature, max_tokens, extra, timeout = spec.call_args(exp)

        def pace():  # before every attempt, retries included
            for key in self._keys(spec):
                if key in self.rpm:
                    self.rpm[key].wait()

        # tokens-per-minute: reserve an estimate (recent average, or prompt size + a margin at first)
        estimate = self.token_estimate.get(spec.name) or (len(json.dumps(messages)) // 3 + min(max_tokens, 800))
        held = [(self.tpm[k], self.tpm[k].reserve(estimate)) for k in self._keys(spec) if k in self.tpm]
        try:
            answer, info = self.clients[spec.provider].complete(
                spec.model, messages, temperature, max_tokens, extra, timeout, before_attempt=pace)
        except RuntimeError as e:
            for limiter, entry in held:
                limiter.settle(entry, 0)
            with self.lock:
                self.streak[spec.name] = self.streak[spec.name] + 1 if "HTTP 429" in str(e) else 0
                if self.streak[spec.name] >= PAUSE_AFTER and spec.name not in self.paused:
                    self.paused.add(spec.name)
                    print(f"  !! {spec.name}: {PAUSE_AFTER} rate-limit failures in a row (quota exhausted?). "
                          f"Pausing it for this run; re-run later to continue.\n"
                          f"     Last error: {str(e)[:400]}")
            raise
        used = info.get("total_tokens") or (info.get("prompt_tokens") or 0) + (info.get("completion_tokens") or 0)
        for limiter, entry in held:
            limiter.settle(entry, used or estimate)
        info["cost"] = spec.cost(info)
        with self.lock:
            self.spent[spec.name] += info["cost"] or 0.0
            self.streak[spec.name] = 0
            if used:
                old = self.token_estimate.get(spec.name)
                self.token_estimate[spec.name] = int(used if old is None else 0.8 * old + 0.2 * used)
        return answer, info


def run_by_model(specs, jobs, fn, default_concurrency):
    """Call fn(job) for every job, using a separate thread pool per model (each job's first element
    is its ModelSpec). All models run at the same time, each with up to `concurrency` calls in
    flight, so a slow or rate-limited model does not hold up the others. Yields results as they finish."""
    pools = {s.name: ThreadPoolExecutor(max_workers=s.concurrency or default_concurrency) for s in specs}
    try:
        futures = [pools[job[0].name].submit(fn, job) for job in jobs]
        for fut in as_completed(futures):
            yield fut.result()
    finally:
        for pool in pools.values():
            pool.shutdown(wait=True)
