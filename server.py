#!/usr/bin/env python3
"""google-play-mcp: Google Play (Android Publisher API v3) を包む最小 MCP サーバー（stdio 専用）。

- 転送: stdio、改行区切り JSON-RPC 2.0（MCP の stdio transport）。ポートは開かない。
- 認証: サービスアカウント鍵ファイルをこのプロセス内でのみ読む。鍵の中身・トークンは出力しない。
- 書き込み道具は play_patch_base_plan_price のみ。edits はコミットしない。

起動:  GOOGLE_PLAY_SA_KEY_FILE=/path/to/service-account.json .venv/bin/python server.py
補助:  server.py --list-tools            道具一覧(JSON)を表示して終了
       server.py --call TOOL 'JSON'      道具を 1 回呼んで結果(JSON)を表示して終了（動作確認用）
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

SERVER_NAME = "google-play-mcp"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {"2025-06-18", "2025-03-26", "2024-11-05"}

SCOPES = ["https://www.googleapis.com/auth/androidpublisher"]
API_BASE = "https://androidpublisher.googleapis.com/androidpublisher/v3"
# 鍵ファイルの「パス」は環境変数 GOOGLE_PLAY_SA_KEY_FILE で受け取る（必須）。
# 鍵の中身は環境変数・引数・リクエスト本文・ログに一切載せない。
KEY_FILE_ENV = "GOOGLE_PLAY_SA_KEY_FILE"
DEFAULT_REGIONS_VERSION = "2022/02"
HTTP_TIMEOUT = 60


def log(msg: str) -> None:
    sys.stderr.write(f"[{SERVER_NAME}] {msg}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------- Google API
class PlayApiError(Exception):
    def __init__(self, status: int, method: str, url: str, body: Any):
        self.status = status
        self.method = method
        self.url = url
        self.body = body
        super().__init__(f"{method} {url} -> HTTP {status}")

    def to_dict(self) -> dict:
        return {"httpStatus": self.status, "method": self.method, "url": self.url, "error": self.body}


class PlayClient:
    def __init__(self, key_file: str):
        if not key_file:
            raise RuntimeError(f"environment variable {KEY_FILE_ENV} is not set (path to the service account key JSON)")
        if not os.path.isfile(key_file):
            raise RuntimeError(f"service account key file not found: {key_file}")
        creds = service_account.Credentials.from_service_account_file(key_file, scopes=SCOPES)
        self.service_account_email = creds.service_account_email
        self.session = AuthorizedSession(creds)

    def request(self, method: str, path: str, *, params: dict | None = None, body: Any = None) -> Any:
        url = f"{API_BASE}/{path.lstrip('/')}"
        resp = self.session.request(method, url, params=params, json=body, timeout=HTTP_TIMEOUT)
        if resp.status_code == 204 or not resp.content:
            payload = None
        else:
            try:
                payload = resp.json()
            except ValueError:
                payload = {"rawText": resp.text[:2000]}
        if resp.status_code >= 400:
            # 送信した URL にはクエリ(updateMask 等)しか無く、資格情報は含まない
            raise PlayApiError(resp.status_code, method, resp.url, payload)
        return payload

    # --- monetization.subscriptions
    def list_subscriptions(self, package: str, page_size: int | None, page_token: str | None) -> dict:
        params: dict[str, Any] = {}
        if page_size:
            params["pageSize"] = page_size
        if page_token:
            params["pageToken"] = page_token
        return self.request("GET", f"applications/{package}/subscriptions", params=params) or {}

    def get_subscription(self, package: str, product_id: str) -> dict:
        return self.request("GET", f"applications/{package}/subscriptions/{product_id}")

    def patch_subscription(self, package: str, product_id: str, body: dict, update_mask: str,
                           regions_version: str, latency_tolerance: str | None) -> dict:
        params = {"updateMask": update_mask, "regionsVersion.version": regions_version}
        if latency_tolerance:
            params["latencyTolerance"] = latency_tolerance
        return self.request("PATCH", f"applications/{package}/subscriptions/{product_id}", params=params, body=body)

    # --- edits（コミットは実装しない）
    def edit_insert(self, package: str) -> dict:
        return self.request("POST", f"applications/{package}/edits", body={})

    def edit_delete(self, package: str, edit_id: str) -> None:
        self.request("DELETE", f"applications/{package}/edits/{edit_id}")

    def track_get(self, package: str, edit_id: str, track: str) -> dict:
        return self.request("GET", f"applications/{package}/edits/{edit_id}/tracks/{track}")


# ---------------------------------------------------------------- helpers
def money_to_str(m: dict | None) -> str | None:
    if not m:
        return None
    units = int(m.get("units", 0) or 0)
    nanos = int(m.get("nanos", 0) or 0)
    cur = m.get("currencyCode", "")
    if nanos == 0:
        return f"{units} {cur}"
    return f"{units + nanos / 1e9:.9g} {cur}"


def base_plan_type(bp: dict) -> dict:
    for key in ("autoRenewingBasePlanType", "prepaidBasePlanType", "installmentsBasePlanType"):
        if key in bp:
            return {"kind": key, **bp[key]}
    return {"kind": "unknown"}


def summarize_base_plan(bp: dict) -> dict:
    regional = []
    for rc in bp.get("regionalConfigs", []) or []:
        regional.append({
            "regionCode": rc.get("regionCode"),
            "newSubscriberAvailability": rc.get("newSubscriberAvailability", False),
            "price": rc.get("price"),
            "priceText": money_to_str(rc.get("price")),
        })
    return {
        "basePlanId": bp.get("basePlanId"),
        "state": bp.get("state"),
        "type": base_plan_type(bp),
        "offerTags": bp.get("offerTags", []),
        "regionalConfigs": regional,
        "otherRegionsConfig": bp.get("otherRegionsConfig"),
    }


def summarize_subscription(sub: dict) -> dict:
    listings = {}
    for ls in sub.get("listings", []) or []:
        listings[ls.get("languageCode")] = {"title": ls.get("title"), "description": ls.get("description")}
    return {
        "packageName": sub.get("packageName"),
        "productId": sub.get("productId"),
        "listings": listings,
        "basePlans": [summarize_base_plan(bp) for bp in sub.get("basePlans", []) or []],
    }


def require(args: dict, name: str, typ=str) -> Any:
    v = args.get(name)
    if v is None or (typ is str and not isinstance(v, str)) or (typ is str and not v):
        raise ValueError(f"missing or invalid required argument: {name}")
    return v


# ---------------------------------------------------------------- tools
TOOLS: list[dict] = [
    {
        "name": "play_list_subscriptions",
        "description": "Google Play の定期購入商品（subscriptions）の一覧を返す（読み取り専用。monetization.subscriptions.list）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "packageName": {"type": "string", "description": "アプリのパッケージ名（例: info.re_x.scam_blocker）"},
                "pageSize": {"type": "integer", "minimum": 1, "maximum": 1000, "description": "1 ページの件数（省略時 API 既定 50）"},
                "pageToken": {"type": "string", "description": "前回の nextPageToken"},
                "raw": {"type": "boolean", "default": False, "description": "true なら API の Subscription リソースをそのまま返す"},
            },
            "required": ["packageName"],
            "additionalProperties": False,
        },
    },
    {
        "name": "play_get_subscription",
        "description": "定期購入商品 1 件を取得し、ベースプランの状態（DRAFT/ACTIVE/INACTIVE）と地域価格を含めて返す（読み取り専用。monetization.subscriptions.get）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "packageName": {"type": "string", "description": "アプリのパッケージ名"},
                "productId": {"type": "string", "description": "商品 ID（例: single_yearly）"},
            },
            "required": ["packageName", "productId"],
            "additionalProperties": False,
        },
    },
    {
        "name": "play_get_track",
        "description": "トラック（production 等）のリリース状態を返す（読み取り専用）。内部で edit を insert → tracks.get → delete し、コミットは絶対にしない。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "packageName": {"type": "string", "description": "アプリのパッケージ名"},
                "track": {"type": "string", "default": "production", "description": "トラック名: production / beta / alpha / internal など（省略時 production）"},
            },
            "required": ["packageName"],
            "additionalProperties": False,
        },
    },
    {
        "name": "play_patch_base_plan_price",
        "description": (
            "【書き込み】ベースプランの地域価格 1 件を更新する（monetization.subscriptions.patch, updateMask=basePlans）。"
            "対象の地域設定が既に存在することと通貨コードの一致を検証し、既定では ACTIVE なベースプランへの書き込みを拒否する。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "packageName": {"type": "string", "description": "アプリのパッケージ名"},
                "productId": {"type": "string", "description": "商品 ID"},
                "basePlanId": {"type": "string", "description": "ベースプラン ID"},
                "regionCode": {"type": "string", "description": "地域コード（例: JP）。既存の regionalConfigs に無い地域は拒否する"},
                "price": {
                    "type": "object",
                    "description": "新しい価格（google.type.Money）。currencyCode は地域の通貨と一致している必要がある",
                    "properties": {
                        "currencyCode": {"type": "string", "description": "ISO 4217（例: JPY）"},
                        "units": {"type": ["string", "integer"], "description": "整数部（int64。文字列でも可）"},
                        "nanos": {"type": "integer", "default": 0, "description": "小数部 ×10^9（-999999999..999999999）"},
                    },
                    "required": ["currencyCode", "units"],
                    "additionalProperties": False,
                },
                "regionsVersion": {"type": "string", "default": DEFAULT_REGIONS_VERSION, "description": "regionsVersion.version（省略時 2022/02）"},
                "latencyTolerance": {
                    "type": "string",
                    "enum": ["PRODUCT_UPDATE_LATENCY_TOLERANCE_LATENCY_SENSITIVE", "PRODUCT_UPDATE_LATENCY_TOLERANCE_LATENCY_TOLERANT"],
                    "description": "省略時は API 既定（LATENCY_SENSITIVE）",
                },
                "allowActive": {"type": "boolean", "default": False, "description": "true のときだけ ACTIVE なベースプランへの書き込みを許す（安全弁）"},
            },
            "required": ["packageName", "productId", "basePlanId", "regionCode", "price"],
            "additionalProperties": False,
        },
    },
]


def tool_list_subscriptions(c: PlayClient, a: dict) -> dict:
    package = require(a, "packageName")
    res = c.list_subscriptions(package, a.get("pageSize"), a.get("pageToken"))
    subs = res.get("subscriptions", []) or []
    out: dict[str, Any] = {"packageName": package, "count": len(subs)}
    out["subscriptions"] = subs if a.get("raw") else [summarize_subscription(s) for s in subs]
    if res.get("nextPageToken"):
        out["nextPageToken"] = res["nextPageToken"]
    return out


def tool_get_subscription(c: PlayClient, a: dict) -> dict:
    sub = c.get_subscription(require(a, "packageName"), require(a, "productId"))
    return {"summary": summarize_subscription(sub), "raw": sub}


def tool_get_track(c: PlayClient, a: dict) -> dict:
    package = require(a, "packageName")
    track = a.get("track") or "production"
    edit = c.edit_insert(package)
    edit_id = edit["id"]
    try:
        tr = c.track_get(package, edit_id, track)
    finally:
        try:
            c.edit_delete(package, edit_id)
            deleted = True
        except Exception as e:  # 削除失敗は結果に残す（edit は未コミットのまま期限切れで消える）
            log(f"edit delete failed: {e}")
            deleted = False
    releases = []
    for r in tr.get("releases", []) or []:
        releases.append({
            "name": r.get("name"),
            "status": r.get("status"),
            "versionCodes": r.get("versionCodes", []),
            "userFraction": r.get("userFraction"),
            "inAppUpdatePriority": r.get("inAppUpdatePriority"),
            "countryTargeting": r.get("countryTargeting"),
            "releaseNotes": r.get("releaseNotes"),
        })
    return {"packageName": package, "track": tr.get("track", track), "releases": releases,
            "edit": {"id": edit_id, "committed": False, "deleted": deleted}}


def tool_patch_base_plan_price(c: PlayClient, a: dict) -> dict:
    package = require(a, "packageName")
    product_id = require(a, "productId")
    base_plan_id = require(a, "basePlanId")
    region = require(a, "regionCode").upper()
    price_in = require(a, "price", dict)
    currency = require(price_in, "currencyCode")
    units = price_in.get("units")
    if units is None:
        raise ValueError("price.units is required")
    units = int(units)
    nanos = int(price_in.get("nanos", 0) or 0)
    if not (-999_999_999 <= nanos <= 999_999_999):
        raise ValueError("price.nanos out of range")
    if units < 0 or nanos < 0:
        raise ValueError("price must be non-negative")
    regions_version = a.get("regionsVersion") or DEFAULT_REGIONS_VERSION
    allow_active = bool(a.get("allowActive", False))

    sub = c.get_subscription(package, product_id)
    base_plans = sub.get("basePlans", []) or []
    target = next((bp for bp in base_plans if bp.get("basePlanId") == base_plan_id), None)
    if target is None:
        raise ValueError(f"basePlanId not found: {base_plan_id} (available: {[bp.get('basePlanId') for bp in base_plans]})")
    state = target.get("state")
    if state == "ACTIVE" and not allow_active:
        raise ValueError(f"base plan {base_plan_id} is ACTIVE; refusing to write (set allowActive=true to override)")
    rc = next((r for r in (target.get("regionalConfigs") or []) if r.get("regionCode") == region), None)
    if rc is None:
        raise ValueError(f"regionCode {region} is not configured on base plan {base_plan_id}; adding regions is not supported by this tool")
    before = dict(rc.get("price") or {})
    before_cur = before.get("currencyCode")
    if before_cur and before_cur != currency:
        raise ValueError(f"currency mismatch: region {region} uses {before_cur}, got {currency}")

    new_price: dict[str, Any] = {"currencyCode": currency, "units": str(units)}
    if nanos:
        new_price["nanos"] = nanos
    rc["price"] = new_price

    body = {"packageName": package, "productId": product_id, "basePlans": base_plans}
    res = c.patch_subscription(package, product_id, body, "basePlans", regions_version, a.get("latencyTolerance"))

    after_bp = next((bp for bp in res.get("basePlans", []) or [] if bp.get("basePlanId") == base_plan_id), {})
    after_rc = next((r for r in (after_bp.get("regionalConfigs") or []) if r.get("regionCode") == region), {})
    return {
        "packageName": package, "productId": product_id, "basePlanId": base_plan_id, "regionCode": region,
        "basePlanState": after_bp.get("state", state),
        "before": {"price": before, "priceText": money_to_str(before)},
        "requested": {"price": new_price, "priceText": money_to_str(new_price)},
        "after": {"price": after_rc.get("price"), "priceText": money_to_str(after_rc.get("price"))},
        "updateMask": "basePlans", "regionsVersion": regions_version,
    }


HANDLERS = {
    "play_list_subscriptions": tool_list_subscriptions,
    "play_get_subscription": tool_get_subscription,
    "play_get_track": tool_get_track,
    "play_patch_base_plan_price": tool_patch_base_plan_price,
}

_client: PlayClient | None = None


def client() -> PlayClient:
    global _client
    if _client is None:
        _client = PlayClient(os.environ.get(KEY_FILE_ENV, ""))
        log("credentials loaded")
    return _client


def call_tool(name: str, args: dict) -> dict:
    """MCP の tools/call 結果（content + structuredContent / isError）を返す。"""
    handler = HANDLERS.get(name)
    if handler is None:
        return {"content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True}
    try:
        result = handler(client(), args or {})
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}],
                "structuredContent": result}
    except PlayApiError as e:
        err = {"error": "google_api_error", **e.to_dict()}
        return {"content": [{"type": "text", "text": json.dumps(err, ensure_ascii=False, indent=2)}], "isError": True}
    except (ValueError, KeyError, TypeError) as e:
        err = {"error": "invalid_request", "message": str(e)}
        return {"content": [{"type": "text", "text": json.dumps(err, ensure_ascii=False)}], "isError": True}
    except Exception as e:  # noqa: BLE001
        log(traceback.format_exc())
        err = {"error": "internal_error", "message": f"{type(e).__name__}: {e}"}
        return {"content": [{"type": "text", "text": json.dumps(err, ensure_ascii=False)}], "isError": True}


# ---------------------------------------------------------------- JSON-RPC (stdio)
def jsonrpc_result(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def jsonrpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def handle_message(msg: dict) -> dict | None:
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}
    is_notification = "id" not in msg

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        return jsonrpc_result(req_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": "Google Play Android Publisher API wrapper. Read tools: play_list_subscriptions, "
                            "play_get_subscription, play_get_track. Write tool: play_patch_base_plan_price only.",
        })
    if method == "notifications/initialized" or (is_notification and method and method.startswith("notifications/")):
        return None
    if method == "ping":
        return jsonrpc_result(req_id, {})
    if method == "tools/list":
        return jsonrpc_result(req_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str):
            return jsonrpc_error(req_id, -32602, "invalid params: name is required")
        return jsonrpc_result(req_id, call_tool(name, args))
    if is_notification:
        return None
    return jsonrpc_error(req_id, -32601, f"method not found: {method}")


def serve_stdio() -> None:
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            resp = jsonrpc_error(None, -32700, f"parse error: {e}")
        else:
            if isinstance(msg, list):  # batch
                resps = [r for r in (handle_message(m) for m in msg if isinstance(m, dict)) if r is not None]
                resp = resps or None
            elif isinstance(msg, dict):
                resp = handle_message(msg)
            else:
                resp = jsonrpc_error(None, -32600, "invalid request")
        if resp is not None:
            out.write(json.dumps(resp, ensure_ascii=False) + "\n")
            out.flush()


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "--list-tools":
        print(json.dumps({"tools": TOOLS}, ensure_ascii=False, indent=2))
        return 0
    if len(argv) >= 3 and argv[1] == "--call":
        args = json.loads(argv[3]) if len(argv) >= 4 else {}
        res = call_tool(argv[2], args)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 1 if res.get("isError") else 0
    serve_stdio()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
