"""Shared utilities for LLM calls in the curation and arrangement pipeline.

Supports four backends:
- claude --print subprocess (default, no API key needed)
- Anthropic SDK (when ANTHROPIC_API_KEY is set, faster for Claude models)
- Google genai SDK (when using gemini-* models, requires GEMINI_API_KEY)
- NVIDIA NIM (OpenAI-compatible, when using nvidia-* models, requires NVIDIA_API_KEY)
"""

import base64
import json
import mimetypes
import os
import re
import subprocess
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Load .env file (if present) — avoids requiring python-dotenv
# ---------------------------------------------------------------------------

def _load_dotenv():
    """Load KEY=VALUE pairs from .env into os.environ (won't overwrite)."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and value and key not in os.environ:
            os.environ[key] = value

_load_dotenv()


# ---------------------------------------------------------------------------
# Model registry & resolution
# ---------------------------------------------------------------------------

MODEL_REGISTRY = {
    # Anthropic aliases
    "sonnet": ("anthropic", "claude-sonnet-4-6"),
    "opus": ("anthropic", "claude-opus-4-6"),
    "haiku": ("anthropic", "claude-haiku-4-5-20251001"),
    # Gemini aliases
    "gemini-flash": ("gemini", "models/gemini-2.5-flash"),
    "gemini-pro": ("gemini", "models/gemini-2.5-pro"),
    # NVIDIA NIM aliases
    "nvidia-glm": ("nvidia", "z-ai/glm4.7"),
    "nvidia-deepseek": ("nvidia", "deepseek-ai/deepseek-v3.2"),
    "nvidia-devstral": ("nvidia", "mistralai/devstral-2-123b-instruct-2512"),
    "nvidia-kimi": ("nvidia", "moonshotai/kimi-k2.5"),
}

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"

# ---------------------------------------------------------------------------
# Stage config — loaded from prompts/llm_config.json
# ---------------------------------------------------------------------------

_LLM_CONFIG_PATH = Path(__file__).parent / "prompts" / "llm_config.json"
_llm_config = {}

def _load_llm_config():
    """Load stage-specific LLM settings from prompts/llm_config.json."""
    global _llm_config
    if _LLM_CONFIG_PATH.exists():
        _llm_config = json.loads(_LLM_CONFIG_PATH.read_text(encoding="utf-8"))

_load_llm_config()

# Fallback system prompt when no stage config is found
_DEFAULT_SYSTEM_PROMPT = (
    "You are an expert assistant for a furniture placement pipeline. "
    "You receive structured prompts with XML-style tags describing tasks, inputs, and output formats. "
    "Follow the output format instructions exactly. Respond with ONLY the requested data "
    "(typically a JSON array or object). No reasoning, no markdown fences, no explanation — "
    "just the raw JSON. Pay close attention to coordinate systems, spatial constraints, "
    "and numeric precision when placing items."
)

def _get_stage_config(stage):
    """Get config dict for a stage, with defaults."""
    cfg = _llm_config.get(stage, {}) if stage else {}
    return {
        "system_prompt": cfg.get("system_prompt", _DEFAULT_SYSTEM_PROMPT),
        "temperature": cfg.get("temperature"),
        "max_tokens": cfg.get("max_tokens", 16384),
    }


def resolve_model(name):
    """Resolve a model name/alias to (provider, full_model_id).

    Accepts aliases from MODEL_REGISTRY or full model IDs directly.
    """
    if name in MODEL_REGISTRY:
        return MODEL_REGISTRY[name]
    # Full ID heuristics
    if name.startswith("gemini"):
        return ("gemini", name)
    if name.startswith("claude"):
        return ("anthropic", name)
    if "/" in name:
        # org/model format — assume NVIDIA NIM
        return ("nvidia", name)
    # Unknown — fall through to CLI which may understand it
    return ("anthropic", name)


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Private backends — all return (raw_text, duration_s, error, usage)
#   usage = {"input_tokens": int, "output_tokens": int} or None
# ---------------------------------------------------------------------------

def _call_claude_cli(prompt, model, timeout):
    """Call claude --print subprocess."""
    cmd = ["claude", "--print", "--model", model]
    t0 = time.time()
    try:
        result = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, round(time.time() - t0, 1), "TIMEOUT", None

    duration = round(time.time() - t0, 1)

    if result.returncode != 0:
        err = f"exit={result.returncode}: {result.stderr.strip()[:300]}"
        return result.stdout.strip(), duration, err, None

    text = result.stdout.strip()
    if not text:
        return "", duration, f"EMPTY: {result.stderr.strip()[:300]}", None

    return text, duration, None, None


def _call_anthropic_sdk(prompt, model_id, timeout, stage_cfg=None):
    """Call Anthropic SDK directly."""
    try:
        import anthropic
    except ImportError:
        return None, 0, "Anthropic SDK not installed. Run: pip install anthropic", None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None, 0, "ANTHROPIC_API_KEY not set", None

    cfg = stage_cfg or _get_stage_config(None)
    kwargs = {
        "model": model_id,
        "max_tokens": cfg["max_tokens"],
        "system": cfg["system_prompt"],
        "messages": [{"role": "user", "content": prompt}],
        "timeout": timeout,
    }
    if cfg["temperature"] is not None:
        kwargs["temperature"] = cfg["temperature"]

    client = anthropic.Anthropic(api_key=api_key)
    t0 = time.time()
    try:
        response = client.messages.create(**kwargs)
    except Exception as e:
        return None, round(time.time() - t0, 1), f"ANTHROPIC_ERROR: {e}", None

    duration = round(time.time() - t0, 1)
    text = response.content[0].text if response.content else ""
    usage = None
    if hasattr(response, "usage") and response.usage:
        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
    if not text:
        return "", duration, "EMPTY", usage
    return text, duration, None, usage


def _call_gemini(prompt, model_id, timeout, stage_cfg=None):
    """Call Google genai SDK."""
    try:
        from google import genai
    except ImportError:
        return None, 0, "Google genai SDK not installed. Run: pip install google-genai", None

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None, 0, "GEMINI_API_KEY not set", None

    cfg = stage_cfg or _get_stage_config(None)
    gen_config = {
        "system_instruction": cfg["system_prompt"],
        "httpOptions": {"timeout": timeout * 1000},
        "max_output_tokens": cfg["max_tokens"],
    }
    if cfg["temperature"] is not None:
        gen_config["temperature"] = cfg["temperature"]

    client = genai.Client(api_key=api_key)
    t0 = time.time()
    try:
        response = client.models.generate_content(
            model=model_id,
            contents=prompt,
            config=genai.types.GenerateContentConfig(**gen_config),
        )
    except Exception as e:
        return None, round(time.time() - t0, 1), f"GEMINI_ERROR: {e}", None

    duration = round(time.time() - t0, 1)
    text = response.text if response.text else ""
    usage = None
    if hasattr(response, "usage_metadata") and response.usage_metadata:
        um = response.usage_metadata
        usage = {
            "input_tokens": getattr(um, "prompt_token_count", 0),
            "output_tokens": getattr(um, "candidates_token_count", 0),
        }
    if not text:
        return "", duration, "EMPTY", usage
    return text, duration, None, usage


def _call_nvidia(prompt, model_id, timeout, stage_cfg=None):
    """Call NVIDIA NIM endpoint (OpenAI-compatible)."""
    try:
        from openai import OpenAI
    except ImportError:
        return None, 0, "OpenAI SDK not installed. Run: pip install openai", None

    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key:
        return None, 0, "NVIDIA_API_KEY not set", None

    cfg = stage_cfg or _get_stage_config(None)
    kwargs = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": cfg["system_prompt"]},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": cfg["max_tokens"],
        "timeout": timeout,
    }
    if cfg["temperature"] is not None:
        kwargs["temperature"] = cfg["temperature"]

    client = OpenAI(base_url=NVIDIA_BASE_URL, api_key=api_key)
    t0 = time.time()
    try:
        response = client.chat.completions.create(**kwargs)
    except Exception as e:
        return None, round(time.time() - t0, 1), f"NVIDIA_ERROR: {e}", None

    duration = round(time.time() - t0, 1)
    text = response.choices[0].message.content if response.choices else ""
    usage = None
    if hasattr(response, "usage") and response.usage:
        usage = {
            "input_tokens": response.usage.prompt_tokens,
            "output_tokens": response.usage.completion_tokens,
        }
    if not text:
        return "", duration, "EMPTY", usage
    return text, duration, None, usage


# ---------------------------------------------------------------------------
# Vision backends — all return (raw_text, duration_s, error)
# ---------------------------------------------------------------------------

def _call_claude_cli_vision(prompt, image_path, model, timeout):
    """Call claude --print with image via Read tool (existing approach)."""
    cmd = ["claude", "--print", "--model", model, "--allowedTools", "Read"]
    t0 = time.time()
    try:
        result = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            encoding="utf-8", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, round(time.time() - t0, 1), "TIMEOUT"

    duration = round(time.time() - t0, 1)

    if result.returncode != 0:
        err = f"exit={result.returncode}: {result.stderr.strip()[:300]}"
        return result.stdout.strip(), duration, err

    text = result.stdout.strip()
    if not text:
        return "", duration, f"EMPTY: {result.stderr.strip()[:300]}"

    return text, duration, None


def _read_image_base64(image_path):
    """Read an image file and return (base64_data, media_type)."""
    mime_type, _ = mimetypes.guess_type(str(image_path))
    if not mime_type:
        mime_type = "image/jpeg"
    with open(image_path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("ascii")
    return data, mime_type


def _call_anthropic_sdk_vision(prompt, image_path, model_id, timeout, stage_cfg=None):
    """Call Anthropic SDK with an image."""
    try:
        import anthropic
    except ImportError:
        return None, 0, "Anthropic SDK not installed. Run: pip install anthropic"

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None, 0, "ANTHROPIC_API_KEY not set"

    cfg = stage_cfg or _get_stage_config(None)
    image_data, media_type = _read_image_base64(image_path)

    kwargs = {
        "model": model_id,
        "max_tokens": cfg["max_tokens"],
        "system": cfg["system_prompt"],
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": image_data,
                    },
                },
                {
                    "type": "text",
                    "text": prompt,
                },
            ],
        }],
        "timeout": timeout,
    }
    if cfg["temperature"] is not None:
        kwargs["temperature"] = cfg["temperature"]

    client = anthropic.Anthropic(api_key=api_key)
    t0 = time.time()
    try:
        response = client.messages.create(**kwargs)
    except Exception as e:
        return None, round(time.time() - t0, 1), f"ANTHROPIC_ERROR: {e}"

    duration = round(time.time() - t0, 1)
    text = response.content[0].text if response.content else ""
    if not text:
        return "", duration, "EMPTY"
    return text, duration, None


def _call_gemini_vision(prompt, image_path, model_id, timeout, stage_cfg=None):
    """Call Google genai SDK with an image."""
    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError:
        return None, 0, "Google genai SDK not installed. Run: pip install google-genai"

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None, 0, "GEMINI_API_KEY not set"

    cfg = stage_cfg or _get_stage_config(None)
    image_data, media_type = _read_image_base64(image_path)

    gen_config = {
        "system_instruction": cfg["system_prompt"],
        "httpOptions": {"timeout": timeout * 1000},
        "max_output_tokens": cfg["max_tokens"],
    }
    if cfg["temperature"] is not None:
        gen_config["temperature"] = cfg["temperature"]

    client = genai.Client(api_key=api_key)
    t0 = time.time()
    try:
        response = client.models.generate_content(
            model=model_id,
            contents=[
                genai_types.Part.from_bytes(
                    data=base64.standard_b64decode(image_data),
                    mime_type=media_type,
                ),
                prompt,
            ],
            config=genai.types.GenerateContentConfig(**gen_config),
        )
    except Exception as e:
        return None, round(time.time() - t0, 1), f"GEMINI_ERROR: {e}"

    duration = round(time.time() - t0, 1)
    text = response.text if response.text else ""
    if not text:
        return "", duration, "EMPTY"
    return text, duration, None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _pick_backend(model):
    """Determine which backend to use for a model name.

    Returns (provider, model_id) where provider is one of:
    "cli", "anthropic", "gemini", "nvidia".
    """
    provider, model_id = resolve_model(model)

    if provider == "gemini":
        return "gemini", model_id

    if provider == "nvidia":
        return "nvidia", model_id

    # Anthropic: prefer SDK if API key is available
    if provider == "anthropic" and os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic", model_id

    # Fallback to CLI — pass the original name so claude CLI can resolve it
    return "cli", model


def _verbose_log(text, duration):
    """Print verbose response summary."""
    print(f"\n    --- raw response ({len(text)} chars, {duration}s) ---")
    print(f"    {text[:1000]}")
    if len(text) > 1000:
        print(f"    ... ({len(text) - 1000} more chars)")
    print(f"    --- end ---")


def call_llm(prompt, model="sonnet", verbose=False, timeout=300, stage=None):
    """Call an LLM with a text prompt. Routes to the appropriate backend.

    Args:
        stage: Stage name (e.g. "curate", "arrange", "profile") to load
               per-stage settings from prompts/llm_config.json.

    Returns (parsed, raw_response, duration_s, error, usage).
    usage is {"input_tokens": int, "output_tokens": int} or None.
    """
    backend, model_id = _pick_backend(model)
    stage_cfg = _get_stage_config(stage)

    if backend == "gemini":
        text, duration, error, usage = _call_gemini(prompt, model_id, timeout, stage_cfg)
    elif backend == "nvidia":
        text, duration, error, usage = _call_nvidia(prompt, model_id, timeout, stage_cfg)
    elif backend == "anthropic":
        text, duration, error, usage = _call_anthropic_sdk(prompt, model_id, timeout, stage_cfg)
    else:
        text, duration, error, usage = _call_claude_cli(prompt, model, timeout)

    if error:
        return None, text, duration, error, usage

    if not text:
        return None, "", duration, "EMPTY", usage

    if verbose:
        _verbose_log(text, duration)

    # Unwrap --output-format json envelope if present
    parsed = extract_json(text)
    if isinstance(parsed, dict) and "result" in parsed and "role" not in parsed and "item_no" not in parsed:
        inner = parsed["result"]
        parsed = extract_json(inner) if isinstance(inner, str) else inner

    if parsed is not None:
        return parsed, text, duration, None, usage

    return None, text, duration, "PARSE_ERROR", usage


def call_llm_vision(prompt, image_path, model="sonnet", verbose=False, timeout=120, stage=None):
    """Call an LLM with a text prompt + image. Routes to the appropriate backend.

    For SDK backends, the image is sent directly in the API call and the
    "Use your Read tool..." line is stripped from the prompt.
    For the CLI backend, the prompt is sent as-is (it tells the LLM to Read the file).

    Args:
        stage: Stage name (e.g. "profile") to load per-stage settings
               from prompts/llm_config.json.

    Returns (parsed, raw_response, duration_s, error).
    """
    backend, model_id = _pick_backend(model)
    stage_cfg = _get_stage_config(stage)

    if backend == "nvidia":
        return None, None, 0, "NVIDIA NIM does not support vision. Use a Claude or Gemini model for image profiling."

    if backend != "cli":
        # Strip the CLI-specific Read tool instruction from the prompt
        prompt = re.sub(
            r"Use your Read tool to view the product image at [^\n]*\n?",
            "",
            prompt,
        )

    if backend == "gemini":
        text, duration, error = _call_gemini_vision(prompt, image_path, model_id, timeout, stage_cfg)
    elif backend == "anthropic":
        text, duration, error = _call_anthropic_sdk_vision(prompt, image_path, model_id, timeout, stage_cfg)
    else:
        text, duration, error = _call_claude_cli_vision(prompt, image_path, model, timeout)

    if error:
        return None, text, duration, error

    if not text:
        return None, "", duration, "EMPTY"

    if verbose:
        _verbose_log(text, duration)

    # Unwrap envelope if present
    parsed = extract_json(text)
    if isinstance(parsed, dict) and "result" in parsed and "tier" not in parsed:
        inner = parsed["result"]
        parsed = extract_json(inner) if isinstance(inner, str) else inner

    if parsed is not None:
        return parsed, text, duration, None

    return None, text, duration, "PARSE_ERROR"
