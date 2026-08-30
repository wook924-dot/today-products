from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse

import requests


API_HOST = "https://api-gateway.coupang.com"


def _get(path: str, params: dict, access_key: str, secret_key: str) -> dict:
    query = urlencode(params)
    stamp = datetime.now(timezone.utc).strftime("%y%m%dT%H%M%SZ")
    message = stamp + "GET" + path + query
    signature = hmac.new(secret_key.encode(), message.encode(), hashlib.sha256).hexdigest()
    authorization = (
        f"CEA algorithm=HmacSHA256, access-key={access_key}, "
        f"signed-date={stamp}, signature={signature}"
    )
    response = requests.get(
        API_HOST + path,
        params=params,
        headers={"Authorization": authorization, "Content-Type": "application/json"},
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    if str(payload.get("rCode", "0")) not in {"0", "200"}:
        raise RuntimeError(f"Coupang API error: {payload.get('rMessage')}")
    return payload


def _product_rows(payload: dict) -> list[dict]:
    data = payload.get("data") or []
    if isinstance(data, dict):
        data = data.get("productData") or data.get("products") or []
    return [row for row in data if isinstance(row, dict)]


def _is_partner_link(value: object) -> bool:
    host = (urlparse(str(value or "")).hostname or "").lower()
    return host.endswith("coupang.com") or host.endswith("coupangcdn.com")


def _api_proof(raw: dict, product_url: str, endpoint: str) -> dict:
    return {
        "eligible": True,
        "productId": str(raw.get("productId") or ""),
        "productUrl": product_url,
        "sourceEndpoint": endpoint,
        "verifiedAt": datetime.now(timezone.utc).isoformat(),
    }


def _image_data_url(url: str) -> str:
    response = requests.get(url, timeout=45)
    response.raise_for_status()
    mime = response.headers.get("Content-Type", "image/jpeg").split(";")[0]
    return f"data:{mime};base64,{base64.b64encode(response.content).decode()}"
