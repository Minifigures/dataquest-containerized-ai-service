"""Prompt optimization logic, kept away from the HTTP layer.

main.py parses requests and shapes errors. This module turns a prompt into a
better prompt and knows nothing about HTTP.

Two sources are possible. With credentials configured, the prompt goes to an
LLM. Without them, a deterministic rule-based rewriter runs instead, so the
service starts, answers and can be tested with no key and no network. Which one
answered is reported in the response's `source` field rather than hidden,
because a caller that must not ship a rules-based answer needs to be able to
tell.
"""

import json
import urllib.error
import urllib.request

import config

STYLE_GUIDANCE = {
    "concise": "Rewrite it to be shorter and unambiguous, keeping every "
               "requirement.",
    "detailed": "Rewrite it to spell out the context, the expected output "
                "format and the constraints.",
    "formal": "Rewrite it in precise, professional language.",
}

SYSTEM_PROMPT = (
    "You rewrite prompts so a language model follows them more reliably. "
    "Return only the rewritten prompt, with no preamble and no commentary."
)


class UpstreamError(Exception):
    """The LLM provider failed. Distinct from a bug in this service.

    main.py maps this to a 502 rather than a 500, which tells the caller the
    fault was upstream and that a retry may well succeed.
    """


def available_styles():
    return sorted(STYLE_GUIDANCE)


def optimize(prompt, style="concise"):
    """Return (optimized_text, source).

    source is 'llm' or 'rules'. Raises UpstreamError if a provider was
    configured and failed, rather than quietly falling back, because silently
    degrading to the rules path would hide an outage behind plausible output.
    """
    if style not in STYLE_GUIDANCE:
        raise ValueError(
            "Unknown style %r. Available: %s"
            % (style, ", ".join(available_styles())))

    text = prompt.strip()
    if not text:
        raise ValueError("Prompt is empty.")

    if config.has_llm_credentials():
        return _optimize_with_llm(text, style), "llm"
    return _optimize_with_rules(text, style), "rules"


def _optimize_with_llm(prompt, style):
    """Call the provider.

    Uses urllib rather than an SDK so the service has no provider dependency
    and the HTTP mechanics stay visible. This is a synchronous call, which is
    exactly why the route that calls it is a plain `def`: FastAPI runs those in
    a worker thread, so a slow provider does not block the event loop. See
    benchmark_async.py for what happens when that rule is broken.
    """
    payload = {
        "model": config.MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",
             "content": "%s\n\nPrompt to rewrite:\n%s"
                        % (STYLE_GUIDANCE[style], prompt)},
        ],
        "temperature": 0.2,
    }
    request = urllib.request.Request(
        config.OPENAI_BASE_URL.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer %s" % config.OPENAI_API_KEY,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request,
                                    timeout=config.REQUEST_TIMEOUT) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as problem:
        detail = problem.read().decode("utf-8", "replace")[:200]
        raise UpstreamError("Provider returned HTTP %d: %s"
                            % (problem.code, _redact(detail)))
    except urllib.error.URLError as problem:
        raise UpstreamError("Could not reach the provider: %s"
                            % _redact(problem.reason))
    except (ValueError, TimeoutError) as problem:
        raise UpstreamError("Provider response was unusable: %s"
                            % _redact(problem))

    choices = body.get("choices") or []
    if not choices:
        raise UpstreamError("Provider returned no choices.")
    content = choices[0].get("message", {}).get("content")
    if not content:
        raise UpstreamError("Provider returned an empty message.")
    return content.strip()


def _redact(value):
    """Keep the API key out of any error that might be logged."""
    text = str(value)
    if config.OPENAI_API_KEY and len(config.OPENAI_API_KEY) > 6:
        text = text.replace(config.OPENAI_API_KEY, "[redacted]")
    return text


VAGUE_OPENERS = (
    "please ", "can you ", "could you ", "i want you to ", "i need you to ",
    "i would like you to ", "kindly ",
)


def _optimize_with_rules(prompt, style):
    """A deterministic rewriter, used when no provider is configured.

    This is not an LLM and does not pretend to be. It applies a few
    uncontroversial prompt-writing rules so the endpoint returns something
    meaningful and testable offline.
    """
    text = " ".join(prompt.split())

    # Strip repeatedly: "please can you ..." has two openers stacked, and
    # stopping at the first leaves "Can you ..." still in place.
    original_lowered = text.lower()
    changed = True
    while changed:
        changed = False
        lowered = text.lower()
        for opener in VAGUE_OPENERS:
            if lowered.startswith(opener):
                text = text[len(opener):]
                changed = True
                break
    if text:
        text = text[0].upper() + text[1:]

    if not text.endswith((".", "!", "?")):
        text += "."

    additions = []
    if style == "concise":
        additions.append("Answer in at most three sentences.")
    elif style == "detailed":
        additions.append("Explain your reasoning step by step.")
        additions.append("State any assumptions you make.")
    elif style == "formal":
        additions.append("Use precise, professional language.")

    if "example" not in original_lowered:
        additions.append("Include one concrete example.")

    return " ".join([text] + additions)
