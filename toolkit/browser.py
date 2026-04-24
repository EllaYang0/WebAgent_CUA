import os
import re
import time
import json
import yaml
import asyncio
import aiohttp
import tiktoken
import requests
import traceback
from typing import Dict, List, Optional, Union

from toolkit.mcp_client import *
from toolkit.tool_explore import process_response


class Visit:
    tool_schema = {
        "type": "function",
        "function": {
            "name": "visit",
            "description": "Visit the webpage and return a summary of its content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL of the webpage to visit.",
                    },
                    "goal": {
                        "type": "string",
                        "description": "The goal or intent of visiting the webpage.",
                    },
                },
                "required": ["url", "goal"],
            }
        }
    }

    async def call(self, params, **kwargs):
        try:
            if isinstance(params, str):
                params = json.loads(params)
            elif isinstance(params, dict):
                pass
            else:
                raise ValueError
            url = params['url']
            goal = params['goal']
        except:
            return "[visit] Invalid request format: Input must be a JSON object containing `url` and `goal` field."

        try:
            client = kwargs.get('client')
            lock = kwargs.get("lock")
            tokenizer = kwargs.get("tokenizer")
            sem = kwargs.get("sem")
            async with lock:
                response = await client.call_tool('browser_navigate', {'url': url})

            print("[visit] browser_navigate returned:",
                  "isError=", getattr(response, "isError", None),
                  "content_len=", len(getattr(response, "content", []) or []))

            content = getattr(response, "content", None) or []
            if len(content) == 0:
                return "[visit] Visit error: empty response.content (server returned no content)."

            raw_response_text = getattr(content[0], "text", None)
            if raw_response_text is None:
                return "[visit] Visit error: response.content[0] has no .text field."

            if getattr(response, "isError", False):
                return f'[visit] Visit error: {raw_response_text}'

            try:
                response_text, record = await process_response(
                    raw_response_text,
                    goal,
                    os.getenv("SUMMARY_MODEL_NAME", os.getenv("MODEL_NAME")),
                    tokenizer,
                    sem
                )
            except Exception as process_error:
                print(f"[visit] Error in process_response: {repr(process_error)}")
                response_text = (
                    "Evidence in page: \nThe provided webpage content could not be accessed.\n\n"
                    "Summary: \nThe webpage content could not be processed."
                )
                record = []

            response_text = f"The useful information from visiting {url} for user goal '{goal}' as follows: \n\n" + response_text
            return f'[visit] {response_text}', record

        except Exception as e:
            print("\n[visit] Exception:", repr(e))
            print(traceback.format_exc())
            return f"[visit] Visit error: exception in browser_navigate call or parsing response: {repr(e)}"


class Click:
    tool_schema = {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click the identified element based on the reference index and return a summary of the content after clicking. You are only allowed to click items that come from the latest visit/click tool's clickable results (you can find them in the `Evidence in page` section).",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "string",
                        "description": "The unique identifier for the element to be clicked on the current page. You must use a ref taken from a notation like [ref=XXX], where XXX is the unique identifier.",
                    },
                    "goal": {
                        "type": "string",
                        "description": "The goal or intent of performing this click.",
                    },
                },
                "required": ["ref", "goal"],
            }
        }
    }

    async def call(self, params, **kwargs):
        try:
            if isinstance(params, str):
                params = json.loads(params)
            elif isinstance(params, dict):
                pass
            else:
                raise ValueError
            ref = params['ref']    # ✅ 修复：原来错误地读取 params['url']
            goal = params['goal']
        except:
            return "[click] Invalid request format: Input must be a JSON object containing `ref` and `goal` field."

        try:
            client = kwargs.get('client')
            lock = kwargs.get("lock")
            tokenizer = kwargs.get("tokenizer")
            sem = kwargs.get("sem")

            async with lock:
                response = await client.call_tool('browser_click', {'ref': ref, 'element': ''})  # ✅ 修复：原来错误地调用 browser_navigate

            print("[click] browser_click returned:",
                  "isError=", getattr(response, "isError", None),
                  "content_len=", len(getattr(response, "content", []) or []))

            content = getattr(response, "content", None) or []
            if len(content) == 0:
                return "[click] Click error: empty response.content (server returned no content)."

            raw_response_text = getattr(content[0], "text", None)
            if raw_response_text is None:
                return "[click] Click error: response.content[0] has no .text field."

            if getattr(response, "isError", False):
                return f'[click] Click error: {raw_response_text}'

            # Use a broader goal for process_response so the Evidence includes
            # all interactive elements on the page (not just the click target).
            extraction_goal = f"{goal}. Also list all interactive elements (input fields, buttons, dropdowns) currently visible on the page so the user can decide the next action."

            try:
                response_text, record = await process_response(
                    raw_response_text,
                    extraction_goal,
                    os.getenv("SUMMARY_MODEL_NAME", os.getenv("MODEL_NAME")),
                    tokenizer,
                    sem
                )
            except Exception as process_error:
                print(f"[click] Error in process_response: {repr(process_error)}")
                response_text = (
                    "Evidence in page: \nThe provided webpage content could not be accessed.\n\n"
                    "Summary: \nThe webpage content could not be processed."
                )
                record = []

            response_text = f"The useful information after clicking [ref={ref}] for user goal '{goal}' as follows: \n\n" + response_text
            return f'[click] {response_text}', record

        except Exception as e:
            print("\n[click] Exception:", repr(e))
            print(traceback.format_exc())
            return f"[click] Click error: exception in browser_click call or parsing response: {repr(e)}"


class Fill:
    tool_schema = {
        "type": "function",
        "function": {
            "name": "fill",
            "description": "Enter text content into the input field and return the filled state. You are only allowed to fill items that come from the latest visit/click tool's fillable results (you can find them in the `Evidence in page` section).",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "string",
                        "description": "The unique identifier for the element to be filled. You must use a ref taken from a notation like [ref=XXX], where XXX is the unique identifier.",
                    },
                    "text": {
                        "type": "string",
                        "description": "The content entered into the textbox.",
                    },
                },
                "required": ["ref", "text"],
            }
        }
    }

    async def call(self, params, **kwargs):
        try:
            if isinstance(params, str):
                params = json.loads(params)
            elif isinstance(params, dict):
                pass
            else:
                raise ValueError
            ref = params['ref']
            text = params['text']
        except:
            return "[fill] Invalid request format: Input must be a JSON object containing `ref` and `text` fields."

        try:
            client = kwargs.get('client')
            lock = kwargs.get("lock")
            tokenizer = kwargs.get("tokenizer")
            sem = kwargs.get("sem")
            async with lock:
                response = await client.call_tool('browser_type', {
                    'ref': ref,
                    'submit': False,
                    'text': text,
                    'element': ""
                })
            response_text = response.content[0].text
        except:
            return '[fill] Fill error: server-side errors.'

        if response.isError:
            return f'[fill] Fill error: {response_text}'

        # After filling, take a snapshot to capture dropdown/autocomplete options
        try:
            async with lock:
                snapshot_response = await client.call_tool('browser_snapshot', {})
            raw_snapshot = getattr(snapshot_response.content[0], "text", None)
            if raw_snapshot and tokenizer and sem:
                goal = f"Find dropdown or autocomplete options after typing '{text}' into the field"
                processed_text, record = await process_response(
                    raw_snapshot,
                    goal,
                    os.getenv("SUMMARY_MODEL_NAME", os.getenv("MODEL_NAME")),
                    tokenizer,
                    sem
                )
                result_text = f"Successfully filled `{text}` into the field [ref={ref}].\n\n{processed_text}"
                return f'[fill] {result_text}', record
        except Exception as e:
            print(f"[fill] Snapshot after fill failed: {repr(e)}")

        return f'[fill] Successfully filled `{text}` into the field [ref={ref}].'