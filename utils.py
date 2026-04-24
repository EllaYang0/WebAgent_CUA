import os
import ast
import json
import random
import aiohttp
from openai import AsyncOpenAI

# Google ADC 认证
import google.auth
import google.auth.transport.requests

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
                if mode == 'agent':
                    response = await client.chat.completions.create(
                        model=model_name,
                        messages=prompt,
                        temperature=1.0,
                        top_p=0.95,
                        max_tokens=max_tokens
                    )
                else:
                    response = await client.chat.completions.create(
                        model=model_name,
                        messages=prompt,
                        temperature=1.0,
                        top_p=0.95,
                        max_tokens=max_tokens
                    )
                result_text = response.choices[0].message.content

                return result_text

            except Exception as e:
                print(f"[CALL LLM async error] {e}")
                if "time out" not in str(e).lower():
                    max_tokens = max_tokens / 2

    return None


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