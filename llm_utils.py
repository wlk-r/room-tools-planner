"""Shared utilities for LLM calls in the placement pipeline."""

import json
import re
import subprocess
import time


def extract_json(text):
    """Try to parse JSON from LLM response text."""
    text = text.strip()
    # Direct parse
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    # Find first [ or { and match
    for start_char, end_char in [("[", "]"), ("{", "}")]:
        start = text.find(start_char)
        if start < 0:
            continue
        end = text.rfind(end_char)
        if end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    # Unwrap markdown code fences
    fenced = re.search(r"```(?:json)?\s*\n([\s\S]*?)\n```", text)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    return None


def call_llm(prompt, model="sonnet", verbose=False, timeout=300):
    """Call claude --print with prompt via stdin.

    Returns (parsed, raw_response, duration_s, error).
    """
    cmd = ["claude", "--print", "--model", model]

    t0 = time.time()
    try:
        result = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        duration = round(time.time() - t0, 1)
        return None, None, duration, "TIMEOUT"

    duration = round(time.time() - t0, 1)

    if result.returncode != 0:
        err = f"exit={result.returncode}: {result.stderr.strip()[:300]}"
        return None, result.stdout.strip(), duration, err

    text = result.stdout.strip()
    if not text:
        return None, "", duration, f"EMPTY: {result.stderr.strip()[:300]}"

    if verbose:
        print(f"\n    --- raw response ({len(text)} chars, {duration}s) ---")
        print(f"    {text[:1000]}")
        if len(text) > 1000:
            print(f"    ... ({len(text) - 1000} more chars)")
        print(f"    --- end ---")

    # Unwrap --output-format json envelope if present
    parsed = extract_json(text)
    if isinstance(parsed, dict) and "result" in parsed and "role" not in parsed and "item_no" not in parsed:
        inner = parsed["result"]
        parsed = extract_json(inner) if isinstance(inner, str) else inner

    if parsed is not None:
        return parsed, text, duration, None

    return None, text, duration, "PARSE_ERROR"
