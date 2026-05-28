import os
import re
import ast
import time
import json
import yaml
import asyncio
import aiohttp
import tiktoken
import requests
import traceback
import base64
from PIL import Image
import io
from typing import Dict, List, Optional, Union

from toolkit.mcp_client import *
from toolkit.tool_explore import process_response
from utils import lenient_json_extract

# CUA (视觉) 相关配置
WINDOWS_MCP_URL = os.getenv("WINDOWS_MCP_URL", "http://localhost:8015")
MAX_RETRIES = 3


def _pick_sem(sem):
    """上层传来的 sem 可能是 asyncio.Semaphore，也可能是 dict:
    {'session': ..., 'llm': ..., 'tool': ...}（见 infer_async_nestbrowse.py）。
    Gemini 视觉调用属于 LLM 类流量，优先取 'llm'。返回 None 表示不限流。
    """
    if sem is None:
        return None
    if isinstance(sem, dict):
        return sem.get('llm') or sem.get('tool') or sem.get('session')
    return sem


# ============================================================
#  视觉辅助函数（从 browser_cua.py 复用）
# ============================================================

async def find_coordinates(screenshot_b64, description, sem):
    """截图发给 Gemini，找到元素的像素坐标"""
    import google.auth
    import google.auth.transport.requests

    img_data = base64.b64decode(screenshot_b64)
    img = Image.open(io.BytesIO(img_data))
    width, height = img.size
    buffer = io.BytesIO()
    img.save(buffer, format='JPEG', quality=50)
    screenshot_b64 = base64.b64encode(buffer.getvalue()).decode()
    print(f'[find_coordinates] jpeg compressed: {len(screenshot_b64)} chars, size: {width}x{height}')

    creds, project = google.auth.default(
        scopes=['https://www.googleapis.com/auth/cloud-platform']
    )
    req = google.auth.transport.requests.Request()
    creds.refresh(req)
    token = creds.token

    model = os.getenv("MODEL_NAME", "google/gemini-2.0-flash-001")
    project = os.getenv("GCP_PROJECT_ID", "msagentrt")
    url = f"https://aiplatform.googleapis.com/v1beta1/projects/{project}/locations/global/endpoints/openapi/chat/completions"

    payload = {
        "model": model,
        "max_tokens": 100,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{screenshot_b64}"
                        }
                    },
                    {
                        "type": "text",
                        "text": f"""This screenshot is {width}x{height} pixels of a Windows desktop. It shows the whole desktop, but you must ONLY look inside the Edge/Chromium browser's web content area (the rendered web page).

STRICTLY IGNORE (do NOT return coordinates in these regions):
- Windows taskbar at the very bottom (Start button, Search box, pinned apps, tray icons, clock) — this is the most common trap; the taskbar Search input is NOT a web page input
- Desktop wallpaper or any area outside the browser window
- Edge's own UI chrome: title bar, address/URL bar, tab bar, bookmarks bar, extensions, profile icon, menu button
- Any noVNC / QEMU host window frame around the guest

Find this element INSIDE the web page content only: {description}

The coordinates must be within the image bounds (x: 0-{width}, y: 0-{height}) AND inside the browser's web content viewport. If no such element is visible inside the web content area, return {{"x": null, "y": null}}.
Look carefully at the exact pixel position of the center of the element.
Return ONLY a JSON object: {{"x": <integer or null>, "y": <integer or null>}}
Nothing else, no explanation."""
                    }
                ]
            }
        ]
    }

    async def _call():
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                return await resp.json()

    real_sem = _pick_sem(sem)
    if real_sem is not None:
        async with real_sem:
            data = await _call()
    else:
        data = await _call()

    try:
        text = data["choices"][0]["message"]["content"]
        print(f"[verify] raw text: {text}")
        coords = lenient_json_extract(text)
        if coords and 'x' in coords and 'y' in coords:
            x, y = coords['x'], coords['y']
            if x is None or y is None:
                return None, None
            return int(x), int(y)
    except Exception as e:
        print(f"[find_coordinates] parse error: {e}, raw response: {data}")
    return None, None


def _extract_balanced_brace_block(s, start_idx=0):
    """从 start_idx 开始扫描，返回**第一个完整、平衡**的 {...} 块（处理嵌套、字符串内的花括号）。
    没有就返回 None。比贪婪正则 `\\{.*\\}` 安全得多——后者会把 markdown 里的 JS 代码块也吃进来。"""
    n = len(s)
    i = start_idx
    while i < n and s[i] != '{':
        i += 1
    if i >= n:
        return None
    depth = 0
    in_str = None  # 当前字符串引号: '"' / "'" / None
    esc = False
    j = i
    while j < n:
        ch = s[j]
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == in_str:
                in_str = None
        else:
            if ch == '"' or ch == "'":
                in_str = ch
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return s[i:j+1]
        j += 1
    return None


def _parse_dom_focus_payload(text):
    """Playwright MCP 1.60+ 的 browser_evaluate 把返回值渲染在 `### Result` 段下面，后面紧跟
    `### Ran Playwright code` markdown 代码块（里面也有 `{...}`）和 `### Page` 段。
    旧实现用 `re.search(r'\\{.*\\}', text, re.DOTALL)` 贪婪匹配从第一个 `{` 一路吃到最后一个
    `}`，把 JS 源码也卷进来导致 25 次 parse_failed。

    新实现：先锚定 `### Result`（如果有就跳到它后面），再用 _extract_balanced_brace_block
    取第一个**平衡**的 {...} 块；解析按 4 档 fallback 走（JSON → ast.literal_eval → bare-key
    quoting → 双层 unescape）。"""
    if not text:
        return None

    # 优先以 `### Result` 作锚点
    anchor = re.search(r'###\s*Result\b', text)
    start = anchor.end() if anchor else 0
    raw = _extract_balanced_brace_block(text, start)
    if raw is None:
        # 退回老路径：从全文找平衡块
        raw = _extract_balanced_brace_block(text, 0)
    if raw is None:
        return None

    # 1) 直接 JSON
    try:
        obj = json.loads(raw)
        if isinstance(obj, str):
            obj = json.loads(obj)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    # 2) Python literal (单引号 dict / True/False/None)
    try:
        obj = ast.literal_eval(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    # 3) JS 对象字面量 → 把 bare key 加引号，再尝试 JSON
    #    {focused: true, tag: 'INPUT'}  →  {"focused": true, "tag": "INPUT"}
    try:
        quoted = re.sub(r'([\{,]\s*)([A-Za-z_$][\w$]*)\s*:', r'\1"\2":', raw)
        # 把单引号字符串改成双引号 (粗糙但够用：键已经被双引号化了)
        quoted = re.sub(r"'([^'\\]*(?:\\.[^'\\]*)*)'", r'"\1"', quoted)
        obj = json.loads(quoted)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    # 4) 双层转义：把 \" 还原成 " 再 JSON
    try:
        unescaped = raw.encode('utf-8').decode('unicode_escape')
        m2 = re.search(r'\{.*\}', unescaped, re.DOTALL)
        if m2:
            obj = json.loads(m2.group())
            if isinstance(obj, dict):
                return obj
    except Exception:
        pass

    return None


async def check_dom_focus(client, lock):
    """用 Playwright MCP 的 browser_evaluate 查 document.activeElement。
    会穿透 iframe 和 shadow DOM，并等一小段时间让 focus/导航稳定。
    返回 dict：{focused, tag, type, value, editableText, text, html, isInteractive,
              isContentEditable, signature, url, readyState} 或 None。
    """
    if client is None or lock is None:
        return None
    try:
        # JS 直接 return 对象 — 不再 JSON.stringify。
        # 原因：MCP 把 string 返回值渲染成无引号的 JS-object 字面量 (导致 char-1 JSONDecodeError)。
        # 让 Python 端的 _parse_dom_focus_payload 兼容多种格式即可。
        js_code = """async () => {
            const deadline = Date.now() + 1500;
            while (document.readyState !== 'complete' && Date.now() < deadline) {
                await new Promise(r => setTimeout(r, 50));
            }
            let el = document.activeElement;
            while (el) {
                if (el.shadowRoot && el.shadowRoot.activeElement) { el = el.shadowRoot.activeElement; continue; }
                if (el.tagName === 'IFRAME') {
                    try {
                        const inner = el.contentDocument && el.contentDocument.activeElement;
                        if (inner && inner !== el.contentDocument.body) { el = inner; continue; }
                    } catch (e) { /* cross-origin iframe */ }
                }
                break;
            }
            if (!el) return {focused: false, tag: 'none', readyState: document.readyState};
            const interactive = ['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON', 'A'].includes(el.tagName) || el.isContentEditable;
            let editableText = '';
            if (el.isContentEditable) editableText = (el.innerText || el.textContent || '').substring(0, 200);
            else if ('value' in el) editableText = (el.value || '').substring(0, 200);
            const signature = el.tagName + '#' + (el.id || '') + '.' + (el.className || '').toString().substring(0, 40)
                               + '@' + (el.getAttribute('name') || '') + ':' + (el.textContent || '').trim().substring(0, 40);
            return {
                focused: el !== document.body && el.tagName !== 'HTML',
                tag: el.tagName,
                type: el.type || '',
                value: (el.value || '').substring(0, 200),
                editableText: editableText,
                text: (el.textContent || '').substring(0, 100).trim(),
                html: (el.outerHTML || '').substring(0, 300),
                isInteractive: interactive,
                isContentEditable: !!el.isContentEditable,
                signature: signature,
                url: document.location.href,
                readyState: document.readyState
            };
        }"""
        async with lock:
            resp = await client.call_tool('browser_evaluate', {'function': js_code})
        content = getattr(resp, 'content', None) or []
        if not content:
            return None
        text = getattr(content[0], 'text', None)
        obj = _parse_dom_focus_payload(text)
        if obj is None:
            print(f"[check_dom_focus] parse failed; text head: {(text or '')[:200]!r}")
        return obj
    except Exception as e:
        print(f"[check_dom_focus] error: {repr(e)}")
        return None


def judge_click_by_dom(dom_focus, prev_focus=None):
    """Click 的 DOM 判定。返回 (decided, success, reason)。
    要求点击后 focus 发生"有意义的变化"——对比点击前的 signature：
      - focus 迁移到新的 interactive 元素 → 成功
      - focus 依旧停留在点击前同一个元素 → 误判风险高，交给视觉
      - focus 落回 body/html → 可能是导航，交给视觉
    """
    if not dom_focus:
        return False, False, "no DOM focus info"
    prev_sig = (prev_focus or {}).get('signature') if prev_focus else None
    cur_sig = dom_focus.get('signature')
    url_changed = bool(prev_focus) and prev_focus.get('url') != dom_focus.get('url')

    if url_changed:
        # 页面已导航，通常说明 click 命中了 link/button
        return True, True, f"URL changed -> {dom_focus.get('url')}"

    if dom_focus.get('focused') and dom_focus.get('isInteractive'):
        if prev_sig is not None and prev_sig == cur_sig:
            # focus 没动，很可能点在空白处，旧 focus 残留
            return False, False, f"focus unchanged ({dom_focus.get('tag')}), inconclusive"
        tag = dom_focus.get('tag', '?')
        snip = (dom_focus.get('html') or '')[:120]
        return True, True, f"focus moved to interactive {tag}: {snip}"
    return False, False, f"DOM focus on {dom_focus.get('tag', '?')}, inconclusive"


def judge_fill_by_dom(dom_focus, expected_text):
    """Fill 的 DOM 判定。返回 (decided, success, reason)。
    设计原则：只判 **明确成功**，不轻易判失败。
    - 焦点元素是 INPUT / TEXTAREA / contentEditable，且 value/innerText 与 expected 双向 substring 命中
      → (True, True, ...) 明确成功
    - 其它一切（无焦点信息、焦点不在可编辑元素、value 不匹配、value 为空等）
      → (False, False, ...) **uncertain**，交给视觉/上层处理，**不再单独判失败**
    这样可以避免 autocomplete 吞输入、格式化值（日期 / 数字 / 缩写）、focus 跑到 dropdown
    等场景被 DOM 单方面误杀，进而触发破坏性 retry。
    """
    if not dom_focus:
        return False, False, "no DOM focus info"
    tag = (dom_focus.get('tag') or '').upper()
    expected = (expected_text or '').strip()
    if not expected:
        return False, False, "no expected text to verify"
    is_editable = tag in ('INPUT', 'TEXTAREA') or dom_focus.get('isContentEditable')
    if not is_editable:
        return False, False, f"DOM focus on {tag or 'none'}, not editable, inconclusive"
    editable_text = (dom_focus.get('editableText') or dom_focus.get('value') or '').strip()
    if not editable_text:
        return False, False, f"{tag} value empty, inconclusive (autocomplete may have consumed input)"
    e_low = expected.lower()
    a_low = editable_text.lower()
    # 双向 substring：处理 "SFO"→"San Francisco (SFO)" 与 "San Francisco"→"SFO" 这类规范化场景
    if e_low in a_low or a_low in e_low:
        return True, True, f"{tag} value '{editable_text[:60]}' matches expected"
    return False, False, f"{tag}='{editable_text[:60]}' inconclusive vs '{expected[:40]}'"


async def verify_action(screenshot_b64, action_description, expected_result, sem, dom_evidence=None):
    """截图验证：操作后截图，让 Gemini 判断是否成功。
    dom_evidence 可选，是 check_dom_focus 返回的 dict；传入后 Gemini 同时看图 + DOM 证据，判决更稳。
    """
    import google.auth
    import google.auth.transport.requests

    img_data = base64.b64decode(screenshot_b64)
    img = Image.open(io.BytesIO(img_data))
    buffer = io.BytesIO()
    img.save(buffer, format='JPEG', quality=50)
    compressed_b64 = base64.b64encode(buffer.getvalue()).decode()

    creds, project = google.auth.default(
        scopes=['https://www.googleapis.com/auth/cloud-platform']
    )
    req = google.auth.transport.requests.Request()
    creds.refresh(req)
    token = creds.token

    model = os.getenv("MODEL_NAME", "google/gemini-2.0-flash-001")
    project = os.getenv("GCP_PROJECT_ID", "msagentrt")
    url = f"https://aiplatform.googleapis.com/v1beta1/projects/{project}/locations/global/endpoints/openapi/chat/completions"

    payload = {
        "model": model,
        "max_tokens": 400,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{compressed_b64}"
                        }
                    },
                    {
                        "type": "text",
                         "text": f"""I just performed this action: {action_description}
Expected result: {expected_result}

The screenshot is the whole Windows desktop. You MUST judge based ONLY on what is inside the Edge/Chromium browser's web content area.

STRICTLY IGNORE (do NOT use as evidence of success):
- Windows taskbar at the bottom (Start button, Search box, tray, clock) — if the typed text ends up in the taskbar Search box, that is a FAILURE, not success
- Edge's address/URL bar, tab bar, bookmarks bar, or any browser UI chrome
- Desktop wallpaper, noVNC/QEMU host frame, or anything outside the web page

{("DOM evidence (treat this as strong signal, it is ground truth from the live page):\\n" + json.dumps(dom_evidence, ensure_ascii=False)[:800] + "\\n\\n") if dom_evidence else ""}Look at the screenshot (web content area only) and combine with any DOM evidence above. Be strict. Only return success=true if the expected result clearly occurred inside the web page. If the typed text only appears in the Windows taskbar or browser chrome, return success=false. If there's no visible change in the web page, or the action clearly missed its target, return success=false. When screenshot and DOM disagree, DOM wins.

Return ONLY a compact JSON object (keep reason under 20 words): {{"success": true/false, "reason": "brief explanation"}}
Nothing else."""
                    }
                ]
            }
        ]
    }

    async def _call():
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                return await resp.json()

    real_sem = _pick_sem(sem)
    if real_sem is not None:
        async with real_sem:
            data = await _call()
    else:
        data = await _call()

    try:
        text = data["choices"][0]["message"]["content"]
        print(f"[verify] raw text: {text}")
        result = lenient_json_extract(text)
        if result is not None and ('success' in result or 'correct' in result):
            success = bool(result.get('success', result.get('correct', False)))
            reason = result.get('reason') or result.get('reasoning') or 'unknown'
            print(f"[verify] success={success}, reason={reason}")
            return success, reason
    except Exception as e:
        print(f"[verify] parse error: {e}, raw response: {data}")

    return False, "verification parse failed, treating as failure"


async def visual_click(ref, goal, sem, client=None, lock=None):
    """视觉兜底：截图 → Gemini 找坐标 → 点击，带重试。
    - 点击前抓一次 focus 基线（prev_focus），用于判断点击后 focus 是否真的"动了"，防止点空白时旧焦点残留被误判为成功
    - DOM 先判；不确定时再走截图视觉判，并把 DOM 证据一起喂给 Gemini
    - 失败重试时在 find_coordinates prompt 里追加"避开之前坐标"，降低同一点击反复失败的概率
    """
    tried_coords = []
    for attempt in range(1, MAX_RETRIES + 1):
        print(f"[visual_click] Attempt {attempt}/{MAX_RETRIES}")

        # 点击前基线
        prev_focus = await check_dom_focus(client, lock)

        screenshot_response = requests.post(
            f"{WINDOWS_MCP_URL}/tools/state",
            json={"use_vision": True},
            timeout=120
        )
        screenshot_b64 = screenshot_response.json()["result"]["screenshot"]

        avoid = ""
        if tried_coords:
            avoid = " Avoid these pixel positions that already failed: " + ", ".join(
                f"({cx},{cy})" for cx, cy in tried_coords[-3:]
            ) + "."
        x, y = await find_coordinates(
            screenshot_b64,
            f"element with ref={ref}, goal={goal}.{avoid}",
            sem
        )
        if x is None:
            print(f"[visual_click] Attempt {attempt}: could not find coordinates")
            if attempt == MAX_RETRIES:
                return None
            await asyncio.sleep(1)
            continue
        tried_coords.append((int(x), int(y)))

        requests.post(
            f"{WINDOWS_MCP_URL}/tools/click",
            json={"loc": [int(x), int(y)]},
            timeout=120
        )
        print(f"[visual_click] clicked at ({x}, {y})")
        await asyncio.sleep(0.6)

        # ① DOM 焦点校验（check_dom_focus 内部已等 readyState）
        dom_focus = await check_dom_focus(client, lock)
        if dom_focus is not None:
            print(f"[visual_click] DOM focus: tag={dom_focus.get('tag')} "
                  f"interactive={dom_focus.get('isInteractive')} "
                  f"sig={(dom_focus.get('signature') or '')[:100]}")
        decided, dom_success, dom_reason = judge_click_by_dom(dom_focus, prev_focus=prev_focus)

        # 拉验证截图
        verify_response = requests.post(
            f"{WINDOWS_MCP_URL}/tools/state",
            json={"use_vision": True},
            timeout=120
        )
        verify_screenshot = verify_response.json()["result"]["screenshot"]

        if decided:
            success, reason = dom_success, f"DOM: {dom_reason}"
        else:
            # ② DOM 不确定，把 DOM 证据一起喂给 Gemini 判（DOM 权重更高的提示已写在 prompt 里）
            visual_success, visual_reason = await verify_action(
                verify_screenshot,
                f"Clicked on element [ref={ref}] at ({x}, {y})",
                f"The element '{ref}' should be activated/focused for goal: {goal}",
                sem,
                dom_evidence=dom_focus
            )
            success = visual_success
            reason = f"visual+dom: {visual_reason} | {dom_reason}"

        if success:
            print(f"[visual_click] Verification passed ({reason})")
            return verify_screenshot
        else:
            print(f"[visual_click] Verification failed ({reason})")
            if attempt == MAX_RETRIES:
                print(f"[visual_click] All attempts failed")
                return verify_screenshot
            await asyncio.sleep(1)

    return None


async def visual_fill(ref, text, sem, client=None, lock=None):
    """视觉兜底：截图 → Gemini 找坐标 → 输入文字。
    新行为契约（按用户要求）：
      - DOM 判定只承认"明确成功"；其余一切（含 value 不匹配、为空、focus 跑了）一律 uncertain
      - **DOM uncertain 时不再触发 ctrl+a/delete 这种破坏性 retry**，避免误杀已正确填好的值
      - 重试只发生在以下两种"无破坏"情形：
            1) Gemini 找不到坐标（还没开始打字）
            2) 视觉判定 success=False **且** Gemini 给出的失败原因属于"明确强信号"
               （例如 'no input field is visible'）—— 这种情况下重新选坐标再点击+输入
      - DOM uncertain + 视觉 uncertain → 直接返回 True (best-effort)；
        page snapshot 是上层 Fill.call 紧接着会读的 ground truth，由 agent 自己看真相再决定下一步
      - 完全找不到坐标 / 一次都没成功打字 → 返回 False，上层报错给 agent
    """
    tried_coords = []
    typed_at_least_once = False
    last_inconclusive_reason = None

    # 视觉判失败的"强信号"关键词：明显说没找到输入框 / 输入区域不可见
    STRONG_FAIL_KEYWORDS = (
        'no input field', 'input field is not', 'input is not visible',
        'no visible input', 'no editable', 'no text field',
        'taskbar', 'search box at the bottom',  # 误打到 Windows taskbar
    )

    for attempt in range(1, MAX_RETRIES + 1):
        print(f"[visual_fill] Attempt {attempt}/{MAX_RETRIES}")

        screenshot_response = requests.post(
            f"{WINDOWS_MCP_URL}/tools/state",
            json={"use_vision": True},
            timeout=120
        )
        screenshot_b64 = screenshot_response.json()["result"]["screenshot"]

        avoid = ""
        if tried_coords:
            avoid = " Avoid these pixel positions that already failed: " + ", ".join(
                f"({cx},{cy})" for cx, cy in tried_coords[-3:]
            ) + "."
        x, y = await find_coordinates(
            screenshot_b64,
            f"input field with ref={ref}, fill with text: {text}.{avoid}",
            sem
        )
        if x is None or y is None:
            print(f"[visual_fill] Attempt {attempt}: could not find coordinates")
            if attempt == MAX_RETRIES:
                # 一次坐标都没找到 → 真失败
                if not typed_at_least_once:
                    return False
                # 之前打过，本次只是没找到新坐标 → 不算硬失败
                print("[visual_fill] returning best-effort True (typed earlier, no new coords)")
                return True
            await asyncio.sleep(1)
            continue

        ix, iy = int(x), int(y)
        tried_coords.append((ix, iy))

        resp = requests.post(
            f"{WINDOWS_MCP_URL}/tools/type",
            json={"loc": [ix, iy], "text": text},
            timeout=120
        )
        typed_at_least_once = True
        print(f"[visual_fill] filled at ({ix}, {iy})")
        print(f"[visual_fill] type response: {resp.json()}")
        await asyncio.sleep(0.6)

        # ① DOM 判定（新版 judge_fill_by_dom 只输出 success / uncertain）
        dom_focus = await check_dom_focus(client, lock)
        if dom_focus is not None:
            editable = dom_focus.get('editableText') or dom_focus.get('value') or ''
            print(f"[visual_fill] DOM focus: tag={dom_focus.get('tag')} "
                  f"editable='{editable[:80]}'")
        else:
            print(f"[visual_fill] DOM focus: <unavailable>")
        decided, dom_success, dom_reason = judge_fill_by_dom(dom_focus, text)

        if decided and dom_success:
            print(f"[visual_fill] DOM confirmed success ({dom_reason})")
            return True

        # ② DOM uncertain → 视觉判 + DOM 证据
        verify_response = requests.post(
            f"{WINDOWS_MCP_URL}/tools/state",
            json={"use_vision": True},
            timeout=120
        )
        verify_screenshot = verify_response.json()["result"]["screenshot"]
        visual_success, visual_reason = await verify_action(
            verify_screenshot,
            f"Typed '{text}' into input field [ref={ref}] at ({ix}, {iy})",
            f"The text '{text}' should be visible in the input field",
            sem,
            dom_evidence=dom_focus
        )

        if visual_success:
            print(f"[visual_fill] Visual confirmed success ({visual_reason})")
            return True

        # ③ 视觉说失败 + DOM uncertain。判断是不是"强信号"失败
        reason_lo = (visual_reason or '').lower()
        is_strong_fail = any(kw in reason_lo for kw in STRONG_FAIL_KEYWORDS)
        last_inconclusive_reason = f"visual: {visual_reason} | dom: {dom_reason}"

        if is_strong_fail and attempt < MAX_RETRIES:
            # 强信号失败：可以再选个坐标试一次，但 **不做 ctrl+a/delete**
            # 直接重新 find_coordinates → click+type；如果原字段已经有错的输入，
            # OS 级 type 会先 click 再打，足以把焦点切到新位置 / 新字段。
            print(f"[visual_fill] Strong-signal failure, retrying without destructive clear ({last_inconclusive_reason})")
            await asyncio.sleep(1)
            continue

        # uncertain (DOM uncertain + 视觉 uncertain/弱失败) → best-effort 返回 True
        print(f"[visual_fill] Inconclusive ({last_inconclusive_reason}); "
              f"returning best-effort True without destructive retry — "
              f"agent will read the next snapshot to verify")
        return True

    # 极端兜底：循环退出但既没明确成功也没明确失败
    if typed_at_least_once:
        return True
    return False


# ============================================================
#  混合版工具类：DOM 优先，视觉兜底
# ============================================================

class Visit:
    """Visit 只用 DOM，不需要视觉"""
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

            # 如果 agent 只是想"刷新快照看当前页面"（target URL 和当前浏览器 URL 都在同一个
            # google.com/travel/flights SPA 下），不要 browser_navigate——那会把 SPA 重置回
            # homepage，丢掉所有已填表单 / 搜索结果。改成只拉 browser_snapshot。
            def _in_flights(u):
                try:
                    return 'google.com/travel/flights' in (u or '').lower()
                except Exception:
                    return False

            cur_url = None
            try:
                async with lock:
                    loc_resp = await client.call_tool(
                        'browser_evaluate',
                        {'function': '() => document.location.href'}
                    )
                loc_content = getattr(loc_resp, 'content', None) or []
                if loc_content:
                    loc_text = getattr(loc_content[0], 'text', '') or ''
                    m = re.search(r'https?://[^\s"\']+', loc_text)
                    if m:
                        cur_url = m.group(0)
            except Exception as loc_err:
                print(f"[visit] read current URL failed: {repr(loc_err)}")

            same_spa = _in_flights(url) and _in_flights(cur_url)
            if same_spa:
                print(f"[visit] Same-SPA refresh (cur={cur_url}, target={url}) — skipping navigate, snapshot only")
                async with lock:
                    response = await client.call_tool('browser_snapshot', {})
            else:
                async with lock:
                    response = await client.call_tool('browser_navigate', {'url': url})

            print("[visit] returned:",
                  "same_spa=", same_spa,
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

            # Playwright MCP 1.60+ 的 browser_navigate 只返回 snapshot 文件引用
            #   "Snapshot: [Snapshot](.playwright-mcp\\page-xxx.yml)"
            # 而不是内联 YAML。任何 browser_navigate 调用之后都强制再 browser_snapshot 拿带
            # [ref=XXX] 的内联 DOM，否则 process_response 永远看不到 ref。
            # (same_spa 分支已经是 browser_snapshot 了，跳过)
            if not same_spa:
                try:
                    async with lock:
                        snap_resp = await client.call_tool('browser_snapshot', {})
                    snap_content = getattr(snap_resp, "content", None) or []
                    if snap_content and not getattr(snap_resp, "isError", False):
                        snap_text = getattr(snap_content[0], "text", None)
                        if snap_text:
                            raw_response_text = snap_text
                            print("[visit] Post-navigate snapshot via browser_snapshot")
                        else:
                            print("[visit] browser_snapshot returned empty text")
                except Exception as snap_err:
                    print(f"[visit] browser_snapshot after navigate failed: {repr(snap_err)}")

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
    """Click：先用 DOM，DOM 失败则切视觉兜底"""
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
            ref = params['ref']
            goal = params['goal']
        except:
            return "[click] Invalid request format: Input must be a JSON object containing `ref` and `goal` field."

        try:
            client = kwargs.get('client')
            lock = kwargs.get("lock")
            tokenizer = kwargs.get("tokenizer")
            sem = kwargs.get("sem")

            dom_success = False
            raw_response_text = None

            # ========== 点击前：记录当前 URL，用于点击后对比 ==========
            async def _read_url():
                try:
                    async with lock:
                        r = await client.call_tool(
                            'browser_evaluate',
                            {'function': '() => document.location.href'}
                        )
                    c = getattr(r, 'content', None) or []
                    if not c:
                        return None
                    t = getattr(c[0], 'text', '') or ''
                    m = re.search(r'https?://[^\s"\']+', t)
                    return m.group(0) if m else None
                except Exception as e:
                    print(f"[click] read URL failed: {repr(e)}")
                    return None

            url_before = await _read_url()

            # ========== 第一步：尝试 DOM 点击 ==========
            try:
                print(f"[click] Trying DOM click for ref={ref}")
                async with lock:
                    response = await client.call_tool('browser_click', {'ref': ref, 'element': ''})

                content = getattr(response, "content", None) or []
                if len(content) > 0 and not getattr(response, "isError", False):
                    raw_response_text = getattr(content[0], "text", None)
                    if raw_response_text:
                        dom_success = True
                        print(f"[click] DOM click succeeded for ref={ref}")
                    else:
                        print(f"[click] DOM click returned empty text")
                else:
                    error_text = getattr(content[0], "text", "unknown") if content else "empty"
                    print(f"[click] DOM click failed: {error_text}")

            except Exception as dom_error:
                print(f"[click] DOM click exception: {repr(dom_error)}")

            # DOM 点击成功但 response 只是 snapshot 引用（Playwright MCP 1.60+ 默认行为），
            # 主动调 browser_snapshot 拿带 [ref=XXX] 的真实 DOM，保证 agent 下一步能用新 ref
            if dom_success and raw_response_text and '[ref=' not in raw_response_text:
                try:
                    async with lock:
                        snap_resp = await client.call_tool('browser_snapshot', {})
                    snap_content = getattr(snap_resp, "content", None) or []
                    if snap_content and not getattr(snap_resp, "isError", False):
                        snap_text = getattr(snap_content[0], "text", None)
                        if snap_text and '[ref=' in snap_text:
                            raw_response_text = snap_text
                            print("[click] Backfilled inline snapshot via browser_snapshot")
                except Exception as snap_err:
                    print(f"[click] browser_snapshot backfill failed: {repr(snap_err)}")

            # ========== 第二步：DOM 失败，切视觉兜底 ==========
            if not dom_success:
                print(f"[click] Falling back to visual click for ref={ref}")
                visual_result = await visual_click(ref, goal, sem, client=client, lock=lock)

                if visual_result is None:
                    return f"[click] Click error: both DOM and visual click failed for ref={ref}"

                # 视觉点击完成后，优先用 Playwright MCP 重新 snapshot 拿带新 refs 的页面，
                # 保证 agent 下一步仍能用 DOM click/fill。browser_snapshot 失败才退回 Windows MCP state。
                try:
                    async with lock:
                        snap_resp = await client.call_tool('browser_snapshot', {})
                    snap_content = getattr(snap_resp, "content", None) or []
                    if snap_content and not getattr(snap_resp, "isError", False):
                        raw_response_text = getattr(snap_content[0], "text", None)
                        print(f"[click] Refreshed snapshot via browser_snapshot after visual click")
                except Exception as snap_err:
                    print(f"[click] browser_snapshot after visual failed: {repr(snap_err)}")

                if not raw_response_text:
                    print("[click] Falling back to Windows MCP state (no Playwright snapshot)")
                    state_response = requests.post(
                        f"{WINDOWS_MCP_URL}/tools/state",
                        json={"use_vision": False},
                        timeout=120
                    )
                    raw_response_text = state_response.json()["result"]["state"]

            # ========== 点击后：读 URL 并生成 URL-diff 信号喂给 agent ==========
            # 用来打破"点 Search 按钮没反馈 → agent 以为 ref 坏 → 换 ref 再点"的死循环
            url_after = await _read_url()
            goal_l = (goal or '').lower()
            looks_like_submit = any(k in goal_l for k in ('search', 'submit', 'go', 'confirm', 'done', 'apply', 'enter'))

            url_note = ''
            if url_before and url_after:
                if url_before == url_after:
                    if looks_like_submit:
                        url_note = (
                            f"\n\n[URL CHECK] document.location.href did NOT change after this click "
                            f"(still {url_after}). Your click goal '{goal}' suggested it should submit/navigate, "
                            f"but the URL stayed the same — the search was NOT submitted. "
                            f"Do NOT repeat the same click; instead check whether the form has unfilled "
                            f"required fields (return date for round trip, passengers, etc.), or the button "
                            f"you clicked is not the real submit button."
                        )
                    else:
                        url_note = f"\n\n[URL CHECK] URL unchanged ({url_after}) — in-page state change only."
                else:
                    url_note = f"\n\n[URL CHECK] URL changed: {url_before} -> {url_after} (click caused navigation/search)."

            # ========== 第三步：处理页面内容 ==========
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

            response_text = f"The useful information after clicking [ref={ref}] for user goal '{goal}' as follows: \n\n" + response_text + url_note
            return f'[click] {response_text}', record

        except Exception as e:
            print("\n[click] Exception:", repr(e))
            print(traceback.format_exc())
            return f"[click] Click error: exception in browser_click call or parsing response: {repr(e)}"


class Fill:
    """Fill：先用 DOM，DOM 失败则切视觉兜底"""
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

            dom_success = False

            # ========== 第一步：尝试 DOM 填入 ==========
            try:
                print(f"[fill] Trying DOM fill for ref={ref}")
                async with lock:
                    response = await client.call_tool('browser_type', {
                        'ref': ref,
                        'submit': False,
                        'text': text,
                        'element': ""
                    })
                response_text = response.content[0].text

                if not response.isError:
                    dom_success = True
                    print(f"[fill] DOM fill succeeded for ref={ref}")
                else:
                    print(f"[fill] DOM fill failed: {response_text}")

            except Exception as dom_error:
                print(f"[fill] DOM fill exception: {repr(dom_error)}")

            # ========== 第二步：DOM 失败，切视觉兜底 ==========
            if not dom_success:
                print(f"[fill] Falling back to visual fill for ref={ref}")
                visual_result = await visual_fill(ref, text, sem, client=client, lock=lock)

                if not visual_result:
                    return f"[fill] Fill error: both DOM and visual fill failed for ref={ref}"

            # ========== 第三步：获取填入后状态 ==========
            # 无论 DOM 还是视觉路径，都优先用 browser_snapshot 拿带 refs 的新快照，
            # 保证 agent 下一步能继续用 DOM 操作；失败才退回 Windows MCP state。
            raw_snapshot = None
            try:
                async with lock:
                    snapshot_response = await client.call_tool('browser_snapshot', {})
                snap_content = getattr(snapshot_response, "content", None) or []
                if snap_content and not getattr(snapshot_response, "isError", False):
                    raw_snapshot = getattr(snap_content[0], "text", None)
                    print(f"[fill] Refreshed snapshot via browser_snapshot")
            except Exception as e:
                print(f"[fill] browser_snapshot failed: {repr(e)}")

            if raw_snapshot is None:
                print("[fill] Falling back to Windows MCP state (no Playwright snapshot)")
                state_response = requests.post(
                    f"{WINDOWS_MCP_URL}/tools/state",
                    json={"use_vision": False},
                    timeout=120
                )
                raw_snapshot = state_response.json()["result"]["state"]

            try:
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

        except Exception as e:
            print("\n[fill] Exception:", repr(e))
            print(traceback.format_exc())
            return f"[fill] Fill error: {repr(e)}"