"""Gemini API 呼叫 (只有 bot 端會用到)。

沒有新增 pip 依賴: 走 discord.py already 帶進來的 aiohttp, 且刻意延遲 import,
讓 build_request / parse_response 這些純邏輯在沒裝 aiohttp 的環境 (測試) 也能用。

任何失敗都回 None —— 呼叫端會降級成統計卡, 總結功能不會因為 LLM 掛掉就整個不能用。
"""

from __future__ import annotations

import json
import re
from typing import List, Optional, Tuple

# 依序嘗試: 前面的不可用就往後退。新 model 名稱可能還沒開放或打錯, 舊的當保底。
DEFAULT_MODELS = ["gemini-3.1-flash-lite", "gemini-2.5-flash-lite", "gemma-4-31B"]
DEFAULT_MODEL = DEFAULT_MODELS[0]

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
TIMEOUT_SECS = 60
_DETAIL_MAX = 300  # 錯誤內文截斷 (要能貼進 Discord, 又要看得出原因)


def parse_models(raw: Optional[str]) -> List[str]:
    """把設定值解析成依序嘗試的 model 清單 (逗號或空白分隔); 空的話用預設鏈。"""
    parts = [p.strip() for p in re.split(r"[,\s]+", raw or "") if p.strip()]
    return parts or list(DEFAULT_MODELS)


def build_request(model: str, prompt: str) -> Tuple[str, dict]:
    """組出 (url, payload)。api key 不放這裡 —— 走 header, 不進 URL query string。"""
    url = _ENDPOINT.format(model=model or DEFAULT_MODEL)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 800},
    }
    return url, payload


def parse_response(obj) -> Optional[str]:
    """從回應取出摘要文字; 取不到 (被安全機制擋掉/空回應/格式不符) 一律回 None。"""
    if not isinstance(obj, dict):
        return None
    candidates = obj.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    first = candidates[0] if isinstance(candidates[0], dict) else {}
    content = first.get("content") if isinstance(first.get("content"), dict) else {}
    parts = content.get("parts")
    if not isinstance(parts, list):
        return None
    text = "\n".join(str(p.get("text", "")) for p in parts
                     if isinstance(p, dict)).strip()
    return text or None


def _brief(s: str) -> str:
    """把回應內文壓成一行短句, 給錯誤回報用。"""
    s = " ".join(str(s).split())
    return s[:_DETAIL_MAX] + ("…" if len(s) > _DETAIL_MAX else "")


def response_issue(obj) -> str:
    """回應沒有可用文字時, 找出原因 (被安全機制擋 / 長度超限 / 格式不符)。診斷用。"""
    if not isinstance(obj, dict):
        return "回應格式不符"
    fb = obj.get("promptFeedback")
    if isinstance(fb, dict) and fb.get("blockReason"):
        return f"提示被擋下: blockReason={fb['blockReason']}"
    if isinstance(obj.get("error"), dict):
        err = obj["error"]
        return f"API 錯誤: {err.get('status') or ''} {err.get('message') or ''}".strip()
    cands = obj.get("candidates")
    if not cands:
        return "回應沒有 candidates"
    if isinstance(cands, list) and cands and isinstance(cands[0], dict):
        fr = cands[0].get("finishReason")
        if fr and fr != "STOP":
            return f"生成中止: finishReason={fr}"
    return "回應沒有文字內容"


async def call(prompt: str, *, api_key: str,
               model: str = DEFAULT_MODEL) -> Tuple[Optional[str], Optional[str]]:
    """呼叫 Gemini, 回 (摘要文字, 錯誤說明): 成功 -> (text, None), 失敗 -> (None, 原因)。

    全專案唯一一份 HTTP 邏輯。錯誤在這裡「原樣回報」而不是吞掉 ——
    summarize() 拿去吞掉降級, /summary-check 拿去把真正的原因報給使用者。"""
    if not api_key:
        return None, "未設定 GEMINI_API_KEY"
    try:
        import aiohttp  # 延遲載入: 純邏輯測試不需要它
    except ImportError as e:
        return None, f"缺 aiohttp 套件 ({e}); 請重跑 pip install -r requirements.txt"

    url, payload = build_request(model, prompt)
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    try:
        timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as r:
                body = await r.text()
                if r.status != 200:
                    return None, f"HTTP {r.status} — {_brief(body)}"
                try:
                    obj = json.loads(body)
                except Exception:
                    return None, f"回應不是合法 JSON — {_brief(body)}"
                text = parse_response(obj)
                return (text, None) if text else (None, response_issue(obj))
    except Exception as e:
        # 逾時/DNS/TLS/連線被拒都走這 — 型別名稱對診斷很有用, 一起帶上
        return None, f"{type(e).__name__}: {_brief(e) or '(無訊息)'}"


async def call_chain(prompt: str, *, api_key: str,
                     models: List[str] = None) -> Tuple[Optional[str], Optional[str], list]:
    """依序試每個 model, 第一個成功就回: (摘要文字, 實際用的 model, [(model, err) 前面失敗的])。
    全部失敗 -> (None, None, 所有錯誤)。

    為什麼要鏈: model 名稱可能還沒開放/打錯/被下架 (404), 或當下額度用完 (429) ——
    往後退一個通常就能出摘要, 比整個 /summary 降級成統計卡好。
    前面失敗的錯誤即使最後成功了也要帶回去 —— 那代表設定裡有個每次都會白試一輪的
    model, /summary-check 要講得出來。"""
    if not api_key:  # 每個 model 都會回同一句, 沒必要試 N 次
        return None, None, [("(未指定)", "未設定 GEMINI_API_KEY")]
    errors = []
    for m in (models or DEFAULT_MODELS):
        text, err = await call(prompt, api_key=api_key, model=m)
        if text:
            return text, m, errors
        errors.append((m, err))
    return None, None, errors
