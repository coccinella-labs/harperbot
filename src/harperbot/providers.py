# SPDX-License-Identifier: MIT
# Copyright (c) 2026 coccinella-labs

"""Provider registry, auto-resolution, and model discovery.

Pure logic plus thin HTTP/SDK calls, all isolated here so the analysis
flow stays untouched. Every network access degrades to warn-and-continue;
listing can never fail a review.
"""

import logging
import time

# Cache TTL for model listings: fresh enough to catch removals without
# calling the endpoint on every PR event.
LIST_CACHE_TTL_SECONDS = 3600

_cache = {}


def _cached(provider_name, fetcher):
    now = time.monotonic()
    entry = _cache.get(provider_name)
    if entry and now - entry[0] < LIST_CACHE_TTL_SECONDS:
        return entry[1]
    models = fetcher()
    _cache[provider_name] = (now, models)
    return models


PROVIDERS = {
    "gemini": {
        "env_key": "GEMINI_API_KEY",
        "default_model": "gemini-3.8-flash",
    },
    "cerebras": {
        "env_key": "CEREBRAS_API_KEY",
        "default_model": "gpt-oss-120b",
    },
    "openrouter": {
        "env_key": "OPENROUTER_API_KEY",
        "default_model": "openrouter/auto",
    },
}


def resolve_provider(config, environ=None):
    """Return (name, env_key, error). Auto picks the first provider in
    provider_order whose key is set; a specific unknown name is an error."""
    import os
    env = environ if environ is not None else os.environ
    wanted = config.get("provider", "gemini")
    if wanted != "auto":
        if wanted not in PROVIDERS:
            return None, None, f"Unknown provider: {wanted}"
        return wanted, PROVIDERS[wanted]["env_key"], None
    order = config.get("provider_order", ["gemini", "openrouter", "cerebras"])
    for name in order:
        if name in PROVIDERS and env.get(PROVIDERS[name]["env_key"]):
            return name, PROVIDERS[name]["env_key"], None
    return None, None, ("No provider API key configured. Set one of: " +
                        ", ".join(PROVIDERS[n]["env_key"] for n in order
                                  if n in PROVIDERS))


def resolve_model(provider, config):
    """Model string for a resolved provider.

    Explicit "auto" means router on OpenRouter, pinned default elsewhere.
    A missing key keeps today's per-provider pinned defaults, so existing
    configs behave exactly as before.
    """
    if provider == "openrouter":
        if config.get("model", "auto") == "auto":
            return "openrouter/auto"
        return config.get("openrouter_model", "openrouter/auto")
    if provider == "cerebras":
        return config.get("cerebras_model", PROVIDERS["cerebras"]["default_model"])
    if config.get("model", "auto") == "auto":
        return PROVIDERS["gemini"]["default_model"]
    return config.get("model")


def list_models(provider, api_key=None):
    """Model IDs for discovery and validation. Warn path on any failure."""
    if provider == "openrouter":
        return _cached("openrouter", _list_openrouter)
    if provider == "gemini":
        return _cached("gemini", lambda: _list_gemini(api_key))
    if provider == "cerebras":
        return _cached("cerebras", lambda: _list_cerebras(api_key))
    return []


def _list_openrouter():
    import requests
    resp = requests.get("https://openrouter.ai/api/v1/models", timeout=30)
    resp.raise_for_status()
    return [m["id"] for m in resp.json().get("data", []) if m.get("id")]


def _list_gemini(api_key):
    from google.genai import Client
    client = Client(api_key=api_key) if api_key else Client()
    return [m.name.replace("models/", "") for m in client.models.list()]


def _list_cerebras(api_key):
    """No SDK list method and HTTP needs auth: try authenticated fetch,
    else static known models. Either way this never raises."""
    if api_key:
        try:
            import requests
            resp = requests.get(
                "https://api.cerebras.ai/v1/models",
                headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
            resp.raise_for_status()
            return [m["id"] for m in resp.json().get("data", [])
                    if m.get("id")]
        except Exception as e:
            logging.warning(f"Cerebras model listing failed, using static "
                            f"list: {e}")
    return ["gpt-oss-120b"]


def validate_model(provider, model, api_key=None):
    """Warn (never fail) when the configured model is absent from listing."""
    try:
        models = list_models(provider, api_key)
    except Exception as e:
        logging.warning(f"Model listing for {provider} failed, skipping "
                        f"validation: {e}")
        return True
    if not models:
        return True
    if model in models:
        return True
    logging.warning(f"Model {model} not in {provider} listing; proceeding "
                    f"anyway")
    return True


def match_allowed_patterns(model, patterns):
    """Client-side pattern check for validation only, not selection."""
    import fnmatch as _fn
    return any(_fn.fnmatchcase(model, p) for p in (patterns or []))


def openrouter_plugins(config, model_name):
    """Server-side allow-list for openrouter/auto, snake_case over HTTP.

    Returns None unless the model is the auto router: pinned models need
    no routing restrictions.
    """
    if model_name != "openrouter/auto":
        return None
    section = config.get("openrouter", {})
    plugins = [{
        "id": "auto-router",
        "allowed_models": section.get("allowed_models",
                                     ["anthropic/*", "google/*"]),
        "cost_tier": section.get("cost_tier", "medium"),
    }]
    excluded = section.get("excluded_models")
    if excluded:
        plugins[0]["excluded_models"] = excluded
    return plugins
