import time
from collections.abc import Callable
from typing import Any

import httpx

from shared.errors import AgentError, UncertainWrite


class JsonHttp:
    """GETと明示された参照POSTのみ再試行。書き込み結果不明は再送しない。"""

    def __init__(self, client: httpx.Client, sleep: Callable[[float], None] = time.sleep):
        self.client = client
        self.sleep = sleep

    def request(self, method: str, path: str, *, read: bool = False, **kwargs: Any) -> dict:
        safe = method == "GET" or read
        for attempt in range(3):
            try:
                response = self.client.request(method, path, **kwargs)
            except httpx.TransportError:
                if not safe:
                    raise UncertainWrite() from None
                if attempt < 2:
                    self.sleep(2**attempt)
                    continue
                raise AgentError("network", "外部サービスへ接続できません。") from None
            status = response.status_code
            if status in (401, 403):
                raise AgentError("permission", "外部サービスの認証または権限を確認してください。")
            if status in (409, 412):
                raise AgentError("conflict", "更新対象が変更されたか、同じIDが存在します。")
            if status == 404:
                raise AgentError("not_found", "対象が見つかりません。")
            if status == 429 or status >= 500:
                if (safe or status == 429) and attempt < 2:
                    try:
                        delay = float(response.headers.get("Retry-After", str(2**attempt)))
                    except ValueError:
                        delay = 2**attempt
                    if delay > 30:
                        raise AgentError("rate_limit", "レート制限中です。時間を置いてください。")
                    self.sleep(max(0, delay))
                    continue
                if not safe and status >= 500:
                    raise UncertainWrite()
                raise AgentError("rate_limit", "外部サービスが混雑しています。")
            if status >= 400:
                raise AgentError("external_input", "外部サービスが入力を受理しませんでした。")
            try:
                return response.json()
            except ValueError:
                if not safe:
                    raise UncertainWrite() from None
                raise AgentError("invalid_response", "外部サービスの応答形式が不正です。") from None
        raise AssertionError("unreachable")
