import os
import ast
import json
import random
import asyncio
import aiohttp
from openai import AsyncOpenAI

# Google ADC 认证
import google.auth
import google.auth.transport.requests


# Vertex AI Gemini safety settings: open all categories so dataset trajectories
# are not lost to safety-filter `content=null` responses on browsecomp questions
# (about deceased authors, accidents, politics, etc.). See
# https://cloud.google.com/vertex-ai/generative-ai/docs/multimodal/configure-safety-attributes
GEMINI_SAFETY_SETTINGS = [
    {"category": cat, "threshold": "BLOCK_NONE"}
    for cat in (
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_DANGEROUS_CONTENT",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "HARM_CATEGORY_HARASSMENT",
        "HARM_CATEGORY_CIVIC_INTEGRITY",
    )
]

_credentials = None
_auth_req = None

def get_access_token():
    """使用 ADC 自动获取 access token"""
    global _credentials, _auth_req
    if _credentials is None:
        _credentials, _ = google.auth.default(
            scopes=['https://www.googleapis.com/auth/cloud-platform']
        )
        _auth_req = google.auth.transport.requests.Request()

    # 刷新 token（如果过期会自动刷新）
    _credentials.refresh(_auth_req)
    return _credentials.token


async def call_llm(sem, prompt, max_tokens, model_name, client=None, mode='agent'):
    if mode == 'agent':
        LLM_API_KEY = get_access_token()  # 使用 ADC 获取 token
        LLM_BASE_URL = os.getenv('AGENT_LLM_BASE_URL')

    elif mode == 'summary':
        LLM_API_KEY = get_access_token()  # 使用 ADC 获取 token
        LLM_BASE_URL = os.getenv('SUMMARY_LLM_BASE_URL', os.getenv('AGENT_LLM_BASE_URL'))

    else:
        raise ValueError(f"Unsupported mode: {mode}")
        

    async with sem['llm']:
        for retry in range(10):
            max_tokens = int(max_tokens)
            try:
                assert isinstance(prompt, list), "For nest_browse, prompt must be a list of messages"

                client = AsyncOpenAI(
                    api_key=LLM_API_KEY,
                    base_url=LLM_BASE_URL,
                )
                # Gemini OpenAI 兼容模式不支持 stop 和 presence_penalty
                # safety_settings 走 extra_body 透传到 Vertex AI 后端
                response = await client.chat.completions.create(
                    model=model_name,
                    messages=prompt,
                    temperature=1.0,
                    top_p=0.95,
                    max_tokens=max_tokens,
                    extra_body={"safety_settings": GEMINI_SAFETY_SETTINGS},
                )

                # Inspect what Gemini actually returned. Empty/None content with
                # finish_reason='content_filter' (or sometimes 'stop' but content
                # is None) is the safety-filter pattern that wiped 22/50 tasks
                # in the prior browsecomp_first50 run. Don't return None — log
                # and retry with backoff so we get usable trajectories.
                choice = response.choices[0] if response.choices else None
                msg = getattr(choice, 'message', None) if choice else None
                result_text = getattr(msg, 'content', None) if msg else None
                finish_reason = getattr(choice, 'finish_reason', None) if choice else None
                refusal = getattr(msg, 'refusal', None) if msg else None

                if result_text and result_text.strip():
                    return result_text

                # Empty content. Try to surface safety_ratings if present in
                # any of the documented fields (varies by SDK version).
                safety_info = ''
                try:
                    sr = (
                        getattr(msg, 'safety_ratings', None)
                        or getattr(choice, 'safety_ratings', None)
                        or (getattr(response, 'model_extra', None) or {}).get('safety_ratings')
                    )
                    if sr:
                        safety_info = f" safety_ratings={sr}"
                except Exception:
                    pass
                print(
                    f"[CALL LLM empty content] retry={retry} "
                    f"finish_reason={finish_reason!r} refusal={refusal!r}"
                    f"{safety_info}"
                )
                # Exponential backoff capped at 30s. Don't halve max_tokens
                # here — empty content isn't a token-budget problem.
                await asyncio.sleep(min(2 ** retry, 30))
                continue

            except Exception as e:
                print(f"[CALL LLM async error] retry={retry} {e}")
                if "time out" not in str(e).lower():
                    max_tokens = max_tokens / 2
                try:
                    await asyncio.sleep(min(2 ** retry, 30))
                except Exception:
                    pass

    return None


import re as _re_lje


def lenient_json_extract(text):
    """Best-effort extraction of a JSON-shaped dict from LLM output.

    Handles the patterns that broke the original `re.sub` + `json.loads` path:
    - ```json ... ``` fences with the lang tag
    - ``` ... ``` plain fences
    - leading/trailing prose ("Sure, here you go: { ... }")
    - dangling text after the JSON body
    - balanced-brace extraction when there are multiple {...} blocks
    - last-resort regex for `"correct"` / `"success"` boolean and `"reason(ing)"`
      so visual `verify_action` and `evaluate_answer_with_llm` always recover
      a verdict if the LLM stated one in any form.

    Returns a dict (possibly partial) or None if absolutely nothing recoverable.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    s = text.strip()
    if not s:
        return None
    # Strip code fences (greedy on outermost pair).
    fence = _re_lje.match(r'^```(?:json|JSON|Json)?\s*\n?(.*?)\n?```\s*$', s, _re_lje.DOTALL)
    if fence:
        s = fence.group(1).strip()
    else:
        # Just remove a leading ```json line if it exists, and trailing ``` if any.
        s = _re_lje.sub(r'^```(?:json|JSON|Json)?\s*\n?', '', s)
        s = _re_lje.sub(r'\n?```\s*$', '', s)
        s = s.strip()
    # Try direct parse.
    try:
        v = json.loads(s)
        if isinstance(v, dict):
            return v
    except Exception:
        pass
    # Walk the string and find a balanced {...} block. Handles trailing prose
    # and multi-block outputs by returning the first balanced object.
    depth = 0
    start = -1
    for i, ch in enumerate(s):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start != -1:
                candidate = s[start:i + 1]
                try:
                    v = json.loads(candidate)
                    if isinstance(v, dict):
                        return v
                except Exception:
                    # try a few common fixups: single quotes, trailing commas
                    fixed = _re_lje.sub(r',\s*([}\]])', r'\1', candidate)
                    try:
                        v = json.loads(fixed)
                        if isinstance(v, dict):
                            return v
                    except Exception:
                        pass
                start = -1
    # Last resort: harvest fields by regex.
    out = {}
    for key in ('correct', 'success'):
        m = _re_lje.search(rf'"{key}"\s*:\s*(true|false)', s, _re_lje.IGNORECASE)
        if m:
            out[key] = m.group(1).lower() == 'true'
    for key in ('reasoning', 'reason'):
        m = _re_lje.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', s)
        if m:
            out[key] = m.group(1)
    return out or None


def read_jsonl(file_path):
    result = []
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                result.append(json.loads(line))
    return result


def count_tokens(text, tokenizer):
    if isinstance(text, str):
        return len(tokenizer.encode(text))

    # 对于消息列表，将所有内容拼接后估算 token 数
    # tiktoken 没有 apply_chat_template，所以直接拼接内容
    if hasattr(tokenizer, 'apply_chat_template'):
        tokens = tokenizer.apply_chat_template(text, tokenize=True)
        return len(tokens)
    else:
        # tiktoken 模式：拼接所有消息内容
        all_text = ""
        for msg in text:
            all_text += msg.get("role", "") + ": " + msg.get("content", "") + "\n"
        return len(tokenizer.encode(all_text))