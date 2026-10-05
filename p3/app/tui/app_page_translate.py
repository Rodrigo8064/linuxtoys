"""Tradução da descrição da app page (porta do AppPageView da GUI, sem GTK)."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app import appstream_cache


def google_translate_text(text: str, target_language: str) -> str:
    text = str(text or "")
    if not text.strip():
        return text
    data = urlencode(
        {
            "client": "gtx",
            "sl": "auto",
            "tl": target_language,
            "dt": "t",
            "q": text,
        }
    ).encode("utf-8")
    request = Request(
        "https://translate.googleapis.com/translate_a/single",
        data=data,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) LinuxToys/1",
            "Accept": "application/json,text/plain,*/*",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"Google Translate HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(
            f"Google Translate connection failed: {exc.reason}"
        ) from exc

    segments = payload[0] if isinstance(payload, list) and payload else []
    translated = "".join(
        str(seg[0])
        for seg in segments
        if isinstance(seg, list) and seg and seg[0] is not None
    )
    if not translated:
        raise ValueError("Google Translate returned an empty translation")
    return translated


def _translate_spans(spans: Any, target: str) -> list[dict]:
    result = []
    for span in spans or ():
        item = dict(span)
        if "code" not in set(item.get("styles") or ()):
            item["text"] = google_translate_text(item.get("text", ""), target)
        result.append(item)
    return result


def translate_description_blocks(blocks: Any, target: str) -> Any:
    """Aceita blocos AppStream (list) ou texto puro (str)."""
    if isinstance(blocks, str):
        return google_translate_text(blocks, target)
    translated = []
    for block in blocks or ():
        copy_block = {"type": block.get("type")}
        kind = block.get("type")
        if kind == "paragraph":
            copy_block["spans"] = _translate_spans(block.get("spans"), target)
        elif kind in ("unordered_list", "ordered_list"):
            copy_block["items"] = [
                _translate_spans(item, target)
                for item in block.get("items") or ()
            ]
        else:
            copy_block.update(block)
        translated.append(copy_block)
    return translated


def _cache_path(info: dict, blocks: Any, target: str):
    app_id = str(info.get("appstream_id") or info.get("id") or "").strip()
    payload = json.dumps(
        blocks or [], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    key = hashlib.sha256(
        f"{app_id}\0{target}\0{digest}".encode("utf-8")
    ).hexdigest()
    return appstream_cache.CACHE_DIR / "translations" / f"{key}.json"


def load_cached_translation(info: dict, blocks: Any, target: str) -> Any:
    try:
        with open(
            _cache_path(info, blocks, target), "r", encoding="utf-8"
        ) as fh:
            value = json.load(fh)
        return value if isinstance(value, (list, str)) else None
    except (OSError, ValueError, TypeError):
        return None


def save_cached_translation(
    info: dict, blocks: Any, target: str, translated: Any
) -> None:
    path = _cache_path(info, blocks, target)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                translated, fh, ensure_ascii=False, separators=(",", ":")
            )
        os.replace(tmp, path)
    except OSError:
        pass
