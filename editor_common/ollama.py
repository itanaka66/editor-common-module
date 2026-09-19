"""Thin async client for a local Ollama server: text generation (plain,
with usage counts, and streamed) plus embeddings. Shared verbatim by both
editors; each app resolves its own live-config defaults (URL/model, which
can be overridden from a settings screen) and passes them in explicitly.

Retries are for transient network failures (connection refused/reset,
timeout) only — an HTTP error status from Ollama itself (e.g. 404 unknown
model) is retried too since it's usually the model still loading, but this
is capped at a couple of attempts so a genuinely bad request fails fast
rather than hanging the caller for minutes.
"""
import asyncio
import json
import logging

import httpx

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2


async def _post_with_retry(url, json, timeout):
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post(url, json=json)
                r.raise_for_status()
                return r
        except (httpx.TransportError, httpx.HTTPStatusError) as ex:
            last_error = ex
            if attempt == MAX_ATTEMPTS:
                break
            logger.warning(
                'Ollama request to %s failed (attempt %d/%d): %s; retrying in %ds',
                url, attempt, MAX_ATTEMPTS, ex, RETRY_BACKOFF_SECONDS,
            )
            await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise last_error


def _payload(model, prompt, stream, options):
    payload = {'model': model, 'prompt': prompt, 'stream': stream}
    if options:
        payload['options'] = options
    return payload


async def generate(prompt, model, url, timeout=240, options=None):
    base = url.rstrip('/')
    r = await _post_with_retry(base + '/api/generate', _payload(model, prompt, False, options), timeout)
    return r.json().get('response', ''), model


async def generate_with_usage(prompt, model, url, timeout=240, options=None):
    """Same as generate(), but also returns Ollama's own token counts
    (prompt_eval_count/eval_count) so callers can log usage."""
    base = url.rstrip('/')
    r = await _post_with_retry(base + '/api/generate', _payload(model, prompt, False, options), timeout)
    data = r.json()
    usage = {'input_tokens': data.get('prompt_eval_count'), 'output_tokens': data.get('eval_count')}
    return data.get('response', ''), model, usage


async def stream_generate(prompt, model, url, timeout=240, options=None):
    """Yields response text deltas as they arrive, then a final usage dict."""
    base = url.rstrip('/')
    usage = {'input_tokens': None, 'output_tokens': None}
    async with httpx.AsyncClient(timeout=timeout) as c:
        async with c.stream('POST', base + '/api/generate', json=_payload(model, prompt, True, options)) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                if chunk.get('response'):
                    yield {'delta': chunk['response']}
                if chunk.get('done'):
                    usage = {'input_tokens': chunk.get('prompt_eval_count'), 'output_tokens': chunk.get('eval_count')}
    yield {'done': True, 'model': model, 'usage': usage}


async def embed(texts, model, url, timeout=180):
    r = await _post_with_retry(url.rstrip('/') + '/api/embed', {'model': model, 'input': texts}, timeout)
    return r.json()['embeddings']
