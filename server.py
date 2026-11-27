#!/usr/bin/env python3
"""google-play-mcp: Google Play (Android Publisher API v3) を包む最小 MCP サーバー（stdio 専用）。

- 転送: stdio、改行区切り JSON-RPC 2.0（MCP の stdio transport）。ポートは開かない。
- 認証: サービスアカウント鍵ファイルをこのプロセス内でのみ読む。鍵の中身・トークンは出力しない。
- 書き込み道具: play_patch_base_plan_price（価格）と、ストア掲載（listings）系の edit 操作。
- edit の commit は play_commit_edit だけが行い、confirm=true を明示しない限り commit しない。
  それ以外の道具は edit を commit しない（読み取り道具は一時 edit を作って必ず削除する）。

起動:  GOOGLE_PLAY_SA_KEY_FILE=/path/to/service-account.json .venv/bin/python server.py
補助:  server.py --list-tools            道具一覧(JSON)を表示して終了
       server.py --call TOOL 'JSON'      道具を 1 回呼んで結果(JSON)を表示して終了（動作確認用）
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import traceback
from typing import Any

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

SERVER_NAME = "google-play-mcp"
SERVER_VERSION = "0.2.0"
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {"2025-06-18", "2025-03-26", "2024-11-05"}

SCOPES = ["https://www.googleapis.com/auth/androidpublisher"]
API_BASE = "https://androidpublisher.googleapis.com/androidpublisher/v3"
UPLOAD_BASE = "https://androidpublisher.googleapis.com/upload/androidpublisher/v3"
# 鍵ファイルの「パス」は環境変数 GOOGLE_PLAY_SA_KEY_FILE で受け取る（必須）。
# 鍵の中身は環境変数・引数・リクエスト本文・ログに一切載せない。
KEY_FILE_ENV = "GOOGLE_PLAY_SA_KEY_FILE"
DEFAULT_REGIONS_VERSION = "2022/02"
HTTP_TIMEOUT = 60
UPLOAD_TIMEOUT = 300
# ストア掲載の文字数上限（Play Console の制限）
LISTING_LIMITS = {"title": 30, "shortDescription": 80, "fullDescription": 4000}
IMAGE_CONTENT_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
MAX_IMAGE_BYTES = 15 * 1024 * 1024


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

    def request(self, method: str, path: str, *, params: dict | None = None, body: Any = None,
                data: bytes | None = None, content_type: str | None = None, upload: bool = False) -> Any:
        url = f"{UPLOAD_BASE if upload else API_BASE}/{path.lstrip('/')}"
        if data is not None:
            resp = self.session.request(method, url, params=params, data=data,
                                        headers={"Content-Type": content_type or "application/octet-stream"},
                                        timeout=UPLOAD_TIMEOUT)
        else:
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

    # --- edits（commit は edit_commit のみ。呼ぶのは play_commit_edit だけ）
    def edit_insert(self, package: str) -> dict:
        return self.request("POST", f"applications/{package}/edits", body={})

    def edit_get(self, package: str, edit_id: str) -> dict:
        return self.request("GET", f"applications/{package}/edits/{edit_id}")

    def edit_validate(self, package: str, edit_id: str) -> dict:
        return self.request("POST", f"applications/{package}/edits/{edit_id}:validate")

    def edit_commit(self, package: str, edit_id: str, changes_not_sent_for_review: bool) -> dict:
        params = {"changesNotSentForReview": "true"} if changes_not_sent_for_review else None
        return self.request("POST", f"applications/{package}/edits/{edit_id}:commit", params=params)

    def details_get(self, package: str, edit_id: str) -> dict:
        return self.request("GET", f"applications/{package}/edits/{edit_id}/details")

    def listings_list(self, package: str, edit_id: str) -> list[dict]:
        res = self.request("GET", f"applications/{package}/edits/{edit_id}/listings") or {}
        return res.get("listings", []) or []

    def listing_get(self, package: str, edit_id: str, language: str) -> dict:
        return self.request("GET", f"applications/{package}/edits/{edit_id}/listings/{language}")

    def listing_update(self, package: str, edit_id: str, language: str, body: dict) -> dict:
        return self.request("PUT", f"applications/{package}/edits/{edit_id}/listings/{language}", body=body)

    def listing_patch(self, package: str, edit_id: str, language: str, body: dict) -> dict:
        return self.request("PATCH", f"applications/{package}/edits/{edit_id}/listings/{language}", body=body)

    def images_list(self, package: str, edit_id: str, language: str, image_type: str) -> list[dict]:
        res = self.request("GET", f"applications/{package}/edits/{edit_id}/listings/{language}/{image_type}") or {}
        return res.get("images", []) or []

    def images_deleteall(self, package: str, edit_id: str, language: str, image_type: str) -> dict:
        return self.request("DELETE", f"applications/{package}/edits/{edit_id}/listings/{language}/{image_type}") or {}

    def images_upload(self, package: str, edit_id: str, language: str, image_type: str,
                      data: bytes, content_type: str) -> dict:
        return self.request("POST", f"applications/{package}/edits/{edit_id}/listings/{language}/{image_type}",
                            params={"uploadType": "media"}, data=data, content_type=content_type, upload=True)

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
    {
        "name": "play_get_listings",
        "description": (
            "ストア掲載（言語ごとの title / shortDescription / fullDescription と featureGraphic）を返す（読み取り専用。edits.listings.list/get, edits.images.list）。"
            "editId を省略すると一時 edit を insert → 読み取り → delete し、コミットは絶対にしない。"
            "editId を渡すとその edit の中身（未 commit の変更を含む）を読み、edit は削除しない。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "packageName": {"type": "string", "description": "アプリのパッケージ名"},
                "languages": {"type": "array", "items": {"type": "string"},
                              "description": "BCP-47 言語コード（例: ja-JP, en-US）。省略時は全言語"},
                "editId": {"type": "string", "description": "既存の edit ID（省略時は一時 edit を作って削除する）"},
                "includeFeatureGraphic": {"type": "boolean", "default": True,
                                          "description": "true なら各言語の featureGraphic（id / sha1 / sha256 / url）も返す"},
            },
            "required": ["packageName"],
            "additionalProperties": False,
        },
    },
    {
        "name": "play_create_edit",
        "description": "【書き込み準備】新しい app edit を作成して ID を返す（edits.insert）。edit は play_commit_edit で commit するか play_delete_edit で破棄する。",
        "inputSchema": {
            "type": "object",
            "properties": {"packageName": {"type": "string", "description": "アプリのパッケージ名"}},
            "required": ["packageName"],
            "additionalProperties": False,
        },
    },
    {
        "name": "play_update_listings",
        "description": (
            "【書き込み・edit 内】1 つの edit の中で、言語ごとの title / shortDescription / fullDescription を設定する"
            "（edits.listings.update / patch）。3 項目すべて指定した言語は update（無ければ新規追加）、一部だけなら patch（既存言語のみ）。"
            "文字数上限（30 / 80 / 4000）を事前に検証する。commit はしない。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "packageName": {"type": "string", "description": "アプリのパッケージ名"},
                "editId": {"type": "string", "description": "play_create_edit で作った edit ID"},
                "listings": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "language": {"type": "string", "description": "BCP-47 言語コード（例: es-ES, pt-BR, zh-CN）"},
                            "title": {"type": "string", "description": "アプリ名（30 文字以内）"},
                            "shortDescription": {"type": "string", "description": "短い説明（80 文字以内）"},
                            "fullDescription": {"type": "string", "description": "詳しい説明（4000 文字以内）"},
                        },
                        "required": ["language"],
                        "additionalProperties": False,
                    },
                    "description": "言語ごとの掲載内容",
                },
            },
            "required": ["packageName", "editId", "listings"],
            "additionalProperties": False,
        },
    },
    {
        "name": "play_upload_feature_graphic",
        "description": (
            "【書き込み・edit 内】指定言語の featureGraphic（1024x500 の PNG/JPEG）をアップロードする（edits.images.deleteall → upload）。"
            "画像はサーバーが動いているマシン上のファイルパスで渡す。アップロード後に sha256 の一致を検証する。commit はしない。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "packageName": {"type": "string", "description": "アプリのパッケージ名"},
                "editId": {"type": "string", "description": "play_create_edit で作った edit ID"},
                "language": {"type": "string", "description": "BCP-47 言語コード"},
                "filePath": {"type": "string", "description": "画像ファイルの絶対パス（.png / .jpg / .jpeg, 15MB 以下）"},
            },
            "required": ["packageName", "editId", "language", "filePath"],
            "additionalProperties": False,
        },
    },
    {
        "name": "play_validate_edit",
        "description": "edit を検証する（edits.validate）。変更は加えず、commit もしない。commit 前の確認用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "packageName": {"type": "string", "description": "アプリのパッケージ名"},
                "editId": {"type": "string", "description": "edit ID"},
            },
            "required": ["packageName", "editId"],
            "additionalProperties": False,
        },
    },
    {
        "name": "play_commit_edit",
        "description": (
            "【書き込み・公開】edit を commit する（edits.commit）。commit は変更の審査への送信に当たり、取り消せない。"
            "confirm=true を明示しない限り実行しない。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "packageName": {"type": "string", "description": "アプリのパッケージ名"},
                "editId": {"type": "string", "description": "edit ID"},
                "confirm": {"type": "boolean", "description": "true のときだけ commit する（安全弁）"},
                "changesNotSentForReview": {"type": "boolean", "default": False,
                                            "description": "true なら審査へ自動送信せずに commit する（Console から手動で送信が必要になる）"},
            },
            "required": ["packageName", "editId", "confirm"],
            "additionalProperties": False,
        },
    },
    {
        "name": "play_delete_edit",
        "description": "未 commit の edit を破棄する（edits.delete）。edit 内の変更はすべて捨てられる。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "packageName": {"type": "string", "description": "アプリのパッケージ名"},
                "editId": {"type": "string", "description": "edit ID"},
            },
            "required": ["packageName", "editId"],
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


def summarize_image(img: dict) -> dict:
    return {"id": img.get("id"), "sha1": img.get("sha1"), "sha256": img.get("sha256"), "url": img.get("url")}


def summarize_listing(ls: dict) -> dict:
    return {
        "language": ls.get("language"),
        "title": ls.get("title"),
        "shortDescription": ls.get("shortDescription"),
        "fullDescription": ls.get("fullDescription"),
        "video": ls.get("video"),
    }


def tool_get_listings(c: PlayClient, a: dict) -> dict:
    package = require(a, "packageName")
    languages = a.get("languages")
    if languages is not None and (not isinstance(languages, list) or not all(isinstance(x, str) and x for x in languages)):
        raise ValueError("languages must be an array of non-empty strings")
    include_fg = a.get("includeFeatureGraphic", True)
    given_edit = a.get("editId")
    temporary = not given_edit
    edit_id = given_edit or c.edit_insert(package)["id"]
    try:
        details = c.details_get(package, edit_id)
        all_listings = c.listings_list(package, edit_id)
        by_lang = {ls.get("language"): ls for ls in all_listings}
        wanted = languages if languages else sorted(by_lang)
        out_listings = []
        missing = []
        for lang in wanted:
            ls = by_lang.get(lang)
            if ls is None:
                missing.append(lang)
                continue
            item = summarize_listing(ls)
            if include_fg:
                item["featureGraphic"] = [summarize_image(i) for i in c.images_list(package, edit_id, lang, "featureGraphic")]
            out_listings.append(item)
    finally:
        deleted = None
        if temporary:
            try:
                c.edit_delete(package, edit_id)
                deleted = True
            except Exception as e:  # 削除失敗は結果に残す（edit は未コミットのまま期限切れで消える）
                log(f"edit delete failed: {e}")
                deleted = False
    return {
        "packageName": package,
        "defaultLanguage": details.get("defaultLanguage"),
        "availableLanguages": sorted(by_lang),
        "listings": out_listings,
        "missingLanguages": missing,
        "edit": {"id": edit_id, "temporary": temporary, "committed": False, "deleted": deleted},
    }


def tool_create_edit(c: PlayClient, a: dict) -> dict:
    package = require(a, "packageName")
    edit = c.edit_insert(package)
    return {"packageName": package, "editId": edit.get("id"), "expiryTimeSeconds": edit.get("expiryTimeSeconds"),
            "committed": False}


def validate_listing_input(item: Any) -> tuple[str, dict]:
    if not isinstance(item, dict):
        raise ValueError("each listings item must be an object")
    lang = require(item, "language")
    fields: dict[str, str] = {}
    for key, limit in LISTING_LIMITS.items():
        if key not in item:
            continue
        v = item[key]
        if not isinstance(v, str):
            raise ValueError(f"{lang}.{key} must be a string")
        if key != "fullDescription" and not v.strip():
            raise ValueError(f"{lang}.{key} must not be empty")
        if len(v) > limit:
            raise ValueError(f"{lang}.{key} is {len(v)} characters; limit is {limit}")
        fields[key] = v
    unknown = set(item) - {"language", *LISTING_LIMITS}
    if unknown:
        raise ValueError(f"{lang}: unknown fields {sorted(unknown)}")
    if not fields:
        raise ValueError(f"{lang}: nothing to set (give title / shortDescription / fullDescription)")
    return lang, fields


def tool_update_listings(c: PlayClient, a: dict) -> dict:
    package = require(a, "packageName")
    edit_id = require(a, "editId")
    items = require(a, "listings", list)
    if not isinstance(items, list) or not items:
        raise ValueError("listings must be a non-empty array")
    # 先に全件を検証してから書き込む（途中で入力エラーにならないように）
    validated = [validate_listing_input(it) for it in items]
    langs = [lang for lang, _ in validated]
    if len(set(langs)) != len(langs):
        raise ValueError(f"duplicate languages in listings: {langs}")
    existing = {ls.get("language"): ls for ls in c.listings_list(package, edit_id)}
    results = []
    for lang, fields in validated:
        before = existing.get(lang)
        if set(fields) == set(LISTING_LIMITS):
            res = c.listing_update(package, edit_id, lang, {"language": lang, **fields})
            op = "update" if before else "create"
        else:
            if before is None:
                raise ValueError(f"{lang}: new language needs title, shortDescription and fullDescription")
            res = c.listing_patch(package, edit_id, lang, fields)
            op = "patch"
        changed = sorted(k for k in fields if (before or {}).get(k) != fields[k])
        results.append({"language": lang, "operation": op, "changedFields": changed,
                        "after": summarize_listing(res or {})})
    return {"packageName": package, "editId": edit_id, "committed": False, "results": results}


def tool_upload_feature_graphic(c: PlayClient, a: dict) -> dict:
    package = require(a, "packageName")
    edit_id = require(a, "editId")
    lang = require(a, "language")
    path = os.path.expanduser(require(a, "filePath"))
    if not os.path.isabs(path):
        raise ValueError("filePath must be an absolute path")
    ext = os.path.splitext(path)[1].lower()
    content_type = IMAGE_CONTENT_TYPES.get(ext)
    if content_type is None:
        raise ValueError(f"unsupported image type {ext!r} (use .png / .jpg / .jpeg)")
    if not os.path.isfile(path):
        raise ValueError(f"file not found: {path}")
    with open(path, "rb") as f:
        data = f.read()
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"image is {len(data)} bytes; limit is {MAX_IMAGE_BYTES}")
    local_sha256 = hashlib.sha256(data).hexdigest()
    before = [summarize_image(i) for i in c.images_list(package, edit_id, lang, "featureGraphic")]
    c.images_deleteall(package, edit_id, lang, "featureGraphic")
    res = c.images_upload(package, edit_id, lang, "featureGraphic", data, content_type) or {}
    uploaded = summarize_image(res.get("image") or {})
    if uploaded.get("sha256") and uploaded["sha256"] != local_sha256:
        raise RuntimeError(f"sha256 mismatch after upload: local {local_sha256}, server {uploaded['sha256']}")
    return {"packageName": package, "editId": edit_id, "language": lang, "imageType": "featureGraphic",
            "file": {"path": path, "bytes": len(data), "sha256": local_sha256},
            "before": before, "uploaded": uploaded, "committed": False}


def tool_validate_edit(c: PlayClient, a: dict) -> dict:
    package = require(a, "packageName")
    edit_id = require(a, "editId")
    res = c.edit_validate(package, edit_id) or {}
    return {"packageName": package, "editId": edit_id, "valid": True, "edit": res, "committed": False}


def tool_commit_edit(c: PlayClient, a: dict) -> dict:
    package = require(a, "packageName")
    edit_id = require(a, "editId")
    if a.get("confirm") is not True:
        raise ValueError("refusing to commit: set confirm=true to commit this edit (commit sends the changes for review)")
    not_sent = bool(a.get("changesNotSentForReview", False))
    res = c.edit_commit(package, edit_id, not_sent) or {}
    return {"packageName": package, "editId": res.get("id", edit_id), "committed": True,
            "changesNotSentForReview": not_sent, "edit": res}


def tool_delete_edit(c: PlayClient, a: dict) -> dict:
    package = require(a, "packageName")
    edit_id = require(a, "editId")
    c.edit_delete(package, edit_id)
    return {"packageName": package, "editId": edit_id, "deleted": True, "committed": False}


HANDLERS = {
    "play_list_subscriptions": tool_list_subscriptions,
    "play_get_subscription": tool_get_subscription,
    "play_get_track": tool_get_track,
    "play_patch_base_plan_price": tool_patch_base_plan_price,
    "play_get_listings": tool_get_listings,
    "play_create_edit": tool_create_edit,
    "play_update_listings": tool_update_listings,
    "play_upload_feature_graphic": tool_upload_feature_graphic,
    "play_validate_edit": tool_validate_edit,
    "play_commit_edit": tool_commit_edit,
    "play_delete_edit": tool_delete_edit,
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
                            "play_get_subscription, play_get_track, play_get_listings, play_validate_edit. "
                            "Write tools: play_patch_base_plan_price; store listing edits via play_create_edit -> "
                            "play_update_listings / play_upload_feature_graphic -> play_commit_edit (confirm=true; "
                            "commit sends the changes for review) or play_delete_edit.",
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
