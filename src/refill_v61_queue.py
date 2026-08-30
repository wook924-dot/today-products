from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import google.auth
import requests
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from openai import OpenAI

from coupang_products import (
    _api_proof,
    _get,
    _image_data_url,
    _is_partner_link,
    _product_rows,
)
from v61_product_policy import evaluate_item, usable_facts


QUEUE_PATH = Path("docs/v60_queue_coupang.json")
CONFIG_PATH = Path("config/v60.json")
CATALOG_PATH = Path("docs/catalog.json")
STATUS_PATH = Path("docs/v61_refill_status.json")
COUPANG_SEARCH_PATH = "/v2/providers/affiliate_open_api/apis/openapi/products/search"
CJ_API_ROOT = "https://developers.cjdropshipping.com/api2.0/v1"
CJ_DOWNLOAD_HOST = "download-only-api.cjdropshipping.com"
SEED_QUERIES = (
    "new creative kitchen gadget",
    "smart home gadget",
    "automatic cleaning tool",
    "portable home appliance",
    "space saving organizer gadget",
    "interactive pet gadget",
    "creative car gadget",
    "new household invention",
)
BLOCKED_TITLE_WORDS = (
    "hat washer", "cap washer", "microwave cover", "phone holder",
    "clothing", "dress", "necklace", "earring", "cosmetic", "makeup",
    "supplement", "medical", "therapy", "weapon", "gun", "adult",
    "refill", "replacement filter", "case only",
)


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:80] or "product"


def parse_json(text: str) -> dict:
    variants = [
        text.strip(),
        re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I | re.S),
    ]
    first, last = text.find("{"), text.rfind("}")
    if first >= 0 and last > first:
        variants.append(text[first:last + 1])
    for value in variants:
        try:
            result = json.loads(value)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            continue
    raise RuntimeError("OpenAI did not return a JSON object")


def post_json(url: str, payload: dict, headers: dict | None = None) -> dict:
    response = requests.post(
        url,
        json=payload,
        headers={"Content-Type": "application/json", **(headers or {})},
        timeout=60,
    )
    response.raise_for_status()
    result = response.json()
    if result.get("result") is False or str(result.get("code") or "").startswith("16"):
        raise RuntimeError(f"CJ API error: {result.get('message') or result.get('code')}")
    return result


def cj_access_token(api_key: str) -> str:
    payload = post_json(f"{CJ_API_ROOT}/authentication/getAccessToken", {"apiKey": api_key})
    token = str((payload.get("data") or {}).get("accessToken") or "").strip()
    if not token:
        raise RuntimeError(f"CJ access token request failed: {payload.get('message')}")
    return token


def cj_products(token: str, query: str, page_size: int = 30) -> list[dict]:
    params = urlencode({
        "page": 1,
        "size": page_size,
        "keyWord": query,
        "productFlag": 2,
        "features": "enable_video",
        "sort": "desc",
        "orderBy": 3,
    })
    response = requests.get(
        f"{CJ_API_ROOT}/product/listV2?{params}",
        headers={"CJ-Access-Token": token},
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    rows = (payload.get("data") or {}).get("content") or []
    result: list[dict] = []
    for group in rows:
        for product in group.get("productList") or []:
            if product.get("isVideo") or product.get("videoList"):
                result.append(product)
    return result


def cj_videos(token: str, product_id: str) -> list[dict]:
    payload = post_json(
        f"{CJ_API_ROOT}/product/queryVideosByProductId",
        {"productId": product_id},
        {"CJ-Access-Token": token},
    )
    rows = payload.get("data") or []
    return [row for row in rows if isinstance(row, dict)]


def eligible_video(rows: list[dict], minimum_seconds: float) -> dict | None:
    eligible = []
    for row in rows:
        url = str(row.get("videoUrl") or "")
        if (
            str(row.get("videoState")) == "ON_STATE"
            and str(row.get("isFree")) == "1"
            and row.get("isBuy") is True
            and url.startswith(f"https://{CJ_DOWNLOAD_HOST}/")
            and float(row.get("duration") or 0) >= minimum_seconds
            and int(row.get("width") or 0) > 0
            and int(row.get("height") or 0) > 0
        ):
            eligible.append(row)
    if not eligible:
        return None
    eligible.sort(
        key=lambda row: (
            abs(float(row.get("duration") or 0) - 45),
            -(int(row.get("width") or 0) * int(row.get("height") or 0)),
        )
    )
    return eligible[0]


def collect_cj_candidates(
    token: str,
    used_cj_ids: set[str],
    minimum_seconds: float,
    scan_limit: int,
) -> list[dict]:
    unique: dict[str, dict] = {}
    for seed in SEED_QUERIES:
        try:
            rows = cj_products(token, seed)
        except Exception as exc:
            print(f"CJ discovery skipped for {seed!r}: {exc}")
            continue
        for row in rows:
            product_id = str(row.get("id") or "").strip()
            title = " ".join(str(row.get("nameEn") or "").split())
            lowered = title.lower()
            if (
                not product_id
                or product_id in used_cj_ids
                or not title
                or not str(row.get("bigImage") or "").startswith("https://")
                or any(word in lowered for word in BLOCKED_TITLE_WORDS)
            ):
                continue
            unique.setdefault(product_id, {
                "id": product_id,
                "nameEn": title,
                "sku": str(row.get("sku") or ""),
                "bigImage": str(row.get("bigImage") or ""),
                "seed": seed,
            })
            if len(unique) >= scan_limit * 3:
                break
        if len(unique) >= scan_limit * 3:
            break

    candidates = []
    for product in unique.values():
        try:
            video = eligible_video(cj_videos(token, product["id"]), minimum_seconds)
        except Exception as exc:
            print(f"CJ video metadata skipped for {product['id']}: {exc}")
            continue
        if video:
            product["video"] = video
            candidates.append(product)
        if len(candidates) >= scan_limit:
            break
    return candidates


def plan_korean_searches(client: OpenAI, products: list[dict]) -> dict[str, str]:
    if not products:
        return {}
    rows = [{"id": row["id"], "english_title": row["nameEn"]} for row in products]
    prompt = (
        "각 영문 상품명을 쿠팡에서 같은 실물 제품을 찾기 위한 짧은 한국어 검색어로 바꾸세요. "
        "브랜드, 색상, 수량, 과장 표현은 빼고 제품의 핵심 구조와 용도를 나타내는 명사 2~5개만 사용하세요. "
        "원문에 없는 기능을 추가하지 마세요. search_keyword는 35자 이하입니다. "
        "JSON {\"items\":[{\"id\":\"...\",\"search_keyword\":\"...\"}]} 만 반환하세요.\n"
        + json.dumps(rows, ensure_ascii=False)
    )
    response = client.responses.create(
        model=os.getenv("OPENAI_TEXT_MODEL", "gpt-5-mini"),
        input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
    )
    data = parse_json(response.output_text)
    result = {}
    for row in data.get("items") or []:
        if not isinstance(row, dict):
            continue
        product_id = str(row.get("id") or "")
        keyword = " ".join(str(row.get("search_keyword") or "").split())[:35]
        if product_id and keyword:
            result[product_id] = keyword
    return result


def coupang_candidates(
    keyword: str,
    access_key: str,
    secret_key: str,
    sub_id: str,
) -> list[dict]:
    params = {"keyword": keyword[:50], "limit": 10}
    if sub_id:
        params["subId"] = sub_id
    payload = _get(COUPANG_SEARCH_PATH, params, access_key, secret_key)
    result = []
    for raw in _product_rows(payload):
        if (
            str(raw.get("productId") or "").strip()
            and str(raw.get("productImage") or "").startswith("https://")
            and _is_partner_link(raw.get("productUrl"))
        ):
            result.append(raw)
    return result[:8]


def exact_match_and_facts(
    client: OpenAI,
    cj_product: dict,
    keyword: str,
    candidates: list[dict],
) -> dict | None:
    if not candidates:
        return None
    content: list[dict] = [{
        "type": "input_text",
        "text": (
            "CJ 기준 상품과 쿠팡 후보 사진·상품명을 비교하세요. 색상이나 판매 수량 차이는 허용하지만, "
            "형태·부품 배치·작동 방식이 같은 실물 제품일 때만 exact_match=true로 판단하세요. "
            "유사 카테고리나 다른 모델이면 반드시 거절하세요. 확신이 없으면 거절하세요. "
            "일치 시 소비자 대본에 쓸 수 있는 보수적인 한국어 사실 2~3개를 사진과 두 상품명에서만 작성하고, "
            "trend_score(0~40), novelty_score(0~30), demonstration_score(0~20)를 엄격히 채점하세요. "
            "평범하거나 구식이면 점수를 낮추세요. JSON 키는 exact_match, product_id, confidence, facts, "
            "trend_score, novelty_score, demonstration_score, reason만 사용하세요."
        ),
    }, {
        "type": "input_text",
        "text": json.dumps({
            "role": "CJ_REFERENCE",
            "title": cj_product["nameEn"],
            "search_keyword": keyword,
        }, ensure_ascii=False),
    }, {
        "type": "input_image",
        "image_url": _image_data_url(cj_product["bigImage"]),
        "detail": "high",
    }]
    included_ids = set()
    for raw in candidates:
        try:
            image = _image_data_url(str(raw.get("productImage") or ""))
        except Exception:
            continue
        product_id = str(raw.get("productId") or "")
        included_ids.add(product_id)
        content.append({
            "type": "input_text",
            "text": json.dumps({
                "role": "COUPANG_CANDIDATE",
                "product_id": product_id,
                "title": raw.get("productName"),
            }, ensure_ascii=False),
        })
        content.append({"type": "input_image", "image_url": image, "detail": "high"})
    if not included_ids:
        return None
    response = client.responses.create(
        model=os.getenv("OPENAI_VISION_MODEL", os.getenv("OPENAI_TEXT_MODEL", "gpt-5-mini")),
        input=[{"role": "user", "content": content}],
    )
    result = parse_json(response.output_text)
    product_id = str(result.get("product_id") or "")
    confidence = float(result.get("confidence") or 0)
    facts = usable_facts(result.get("facts"))
    trend = max(0, min(40, int(result.get("trend_score") or 0)))
    novelty = max(0, min(30, int(result.get("novelty_score") or 0)))
    demonstration = max(0, min(20, int(result.get("demonstration_score") or 0)))
    if (
        result.get("exact_match") is not True
        or product_id not in included_ids
        or confidence < 0.94
        or len(facts) < 2
        or trend + novelty + demonstration + 10 < 65
    ):
        print(
            f"Rejected visual match for CJ {cj_product['id']}: "
            f"confidence={confidence:.2f}, score={trend + novelty + demonstration + 10}"
        )
        return None
    return {
        "product_id": product_id,
        "confidence": confidence,
        "facts": facts[:3],
        "trendScore": trend,
        "noveltyScore": novelty,
        "demonstrationScore": demonstration,
        "reason": str(result.get("reason") or "")[:300],
    }


def download_video(video: dict, destination: Path) -> str:
    url = str(video.get("videoUrl") or "")
    response = requests.get(
        url,
        headers={"Referer": "https://developers.cjdropshipping.com/"},
        timeout=180,
        stream=True,
    )
    response.raise_for_status()
    digest = hashlib.sha256()
    size = 0
    with destination.open("wb") as handle:
        for chunk in response.iter_content(1024 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > 160 * 1024 * 1024:
                raise RuntimeError("CJ video exceeds 160 MB")
            digest.update(chunk)
            handle.write(chunk)
    if size < 100_000:
        raise RuntimeError("Downloaded CJ video is unexpectedly small")
    return digest.hexdigest()


def upload_drive(path: Path, name: str, folder_id: str) -> str:
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/drive"])
    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    result = service.files().create(
        body={"name": name, "parents": [folder_id]},
        media_body=MediaFileUpload(str(path), mimetype="video/mp4", resumable=True),
        fields="id,name,size,parents",
        supportsAllDrives=True,
    ).execute()
    return str(result["id"])


def product_from_match(raw: dict, now: str) -> dict:
    product_url = str(raw.get("productUrl") or "").strip()
    product_id = str(raw.get("productId") or "").strip()
    return {
        "provider": "coupang",
        "productId": product_id,
        "productName": " ".join(str(raw.get("productName") or "").split()),
        "productPrice": int(raw.get("productPrice") or 0),
        "productImage": str(raw.get("productImage") or ""),
        "productUrl": product_url,
        "categoryName": str(raw.get("categoryName") or "생활용품"),
        "isRocket": bool(raw.get("isRocket")),
        "isFreeShipping": bool(raw.get("isFreeShipping")),
        "sourceProductUrl": f"https://www.coupang.com/vp/products/{product_id}",
        "coupangPartnersApi": _api_proof(raw, product_url, COUPANG_SEARCH_PATH),
        "verifiedAt": now,
        "affiliateDisclosure": (
            "이 콘텐츠는 쿠팡 파트너스 활동의 일환으로, "
            "이에 따른 일정액의 수수료를 제공받습니다."
        ),
    }


def build_queue_item(
    product: dict,
    cj_product: dict,
    match: dict,
    drive_file_id: str,
    drive_file_name: str,
    sha256: str,
    now: str,
) -> dict:
    video = cj_product["video"]
    source = {
        "mode": "google_drive",
        "fileId": drive_file_id,
        "fileName": drive_file_name,
        "sha256": sha256,
        "durationSeconds": float(video.get("duration") or 0),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "exactProductVerified": True,
        "exactMatchConfidence": match["confidence"],
        "rightsApproved": True,
        "sourcePlatform": "CJdropshipping",
        "sourceProductId": cj_product["id"],
        "sourceProductUrl": (
            "https://cjdropshipping.com/product/"
            f"{slugify(cj_product['nameEn'])}-p-{cj_product['id']}.html"
        ),
        "sourceVideoId": str(video.get("videoId") or ""),
        "priority": 1,
        "rightsEvidence": {
            "type": "affiliate_creative_license",
            "reference": (
                "CJ API video metadata: isFree=1 and isBuy=true; "
                "downloaded through official API"
            ),
            "termsReference": "https://developers.cjdropshipping.cn/en/api/api2/api/product.html",
            "cjProductId": cj_product["id"],
            "cjVideoId": str(video.get("videoId") or ""),
        },
    }
    return {
        "id": f"coupang-{product['productId']}-cj-v61",
        "status": "ready",
        "queuedAt": now,
        "product": product,
        "verifiedFacts": match["facts"],
        "discovery": {
            "source": "CJ free video catalog matched to Coupang Partners",
            "discoveredAt": now,
            "trendScore": match["trendScore"],
            "noveltyScore": match["noveltyScore"],
            "demonstrationScore": match["demonstrationScore"],
            "rationale": match["reason"],
        },
        "source": source,
        "sources": [source],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-ready", type=int, default=6)
    parser.add_argument("--max-add", type=int, default=3)
    parser.add_argument("--scan-limit", type=int, default=18)
    parser.add_argument("--artifact-dir", type=Path)
    args = parser.parse_args()

    config = load_json(CONFIG_PATH, {})
    queue = load_json(QUEUE_PATH, {"version": 1, "provider": "coupang", "items": []})
    catalog = load_json(CATALOG_PATH, [])
    now = datetime.now(timezone.utc).isoformat()
    ready_count = sum(1 for row in queue.get("items") or [] if row.get("status") == "ready")
    needed = max(0, min(args.max_add, args.target_ready - ready_count))
    print(f"V61 queue ready={ready_count}, target={args.target_ready}, add_limit={needed}")
    if needed == 0:
        STATUS_PATH.write_text(
            json.dumps({
                "status": "ok",
                "checkedAt": now,
                "readyBefore": ready_count,
                "added": 0,
                "readyAfter": ready_count,
                "reason": "target-already-satisfied",
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return

    used_product_ids = {
        str((row.get("product") or {}).get("productId") or "")
        for row in queue.get("items") or [] if isinstance(row, dict)
    }
    used_product_ids.update(
        str(row.get("productId") or "") for row in catalog if isinstance(row, dict)
    )
    used_cj_ids = {
        str((row.get("source") or {}).get("sourceProductId") or "")
        for row in queue.get("items") or [] if isinstance(row, dict)
    }

    cj_key = os.environ["CJ_API_KEY"].strip()
    coupang_access = os.environ["COUPANG_ACCESS_KEY"].strip()
    coupang_secret = os.environ["COUPANG_SECRET_KEY"].strip()
    coupang_sub_id = os.getenv("COUPANG_SUB_ID", "youtube_shorts").strip()
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"].strip())
    token = cj_access_token(cj_key)
    cj_rows = collect_cj_candidates(
        token,
        used_cj_ids,
        float(config.get("minimumSourceSeconds") or 31),
        max(1, args.scan_limit),
    )
    print(f"CJ candidates with eligible free video: {len(cj_rows)}")
    searches = plan_korean_searches(client, cj_rows)
    folder_id = str(((config.get("googleDrive") or {}).get("providerFolderIds") or {}).get("coupang") or "")
    if not args.artifact_dir and not folder_id:
        raise RuntimeError("Missing Coupang Google Drive folder ID")
    if args.artifact_dir:
        args.artifact_dir.mkdir(parents=True, exist_ok=True)

    added = 0
    for cj_product in cj_rows:
        if added >= needed:
            break
        keyword = searches.get(cj_product["id"])
        if not keyword:
            continue
        try:
            candidates = [
                row for row in coupang_candidates(
                    keyword, coupang_access, coupang_secret, coupang_sub_id
                )
                if str(row.get("productId") or "") not in used_product_ids
            ]
            match = exact_match_and_facts(client, cj_product, keyword, candidates)
            if not match:
                continue
            raw = next(
                row for row in candidates
                if str(row.get("productId") or "") == match["product_id"]
            )
            product = product_from_match(raw, now)
            drive_name = (
                f"v61-cj-coupang-{product['productId']}-"
                f"{cj_product.get('sku') or cj_product['id']}.mp4"
            )
            pending_item = build_queue_item(
                product,
                cj_product,
                match,
                "pending-upload",
                drive_name,
                "0" * 64,
                now,
            )
            approved, score, reason = evaluate_item(pending_item, config)
            if not approved:
                print(f"Rejected final V61 item {pending_item['id']}: {reason} ({score})")
                continue
            with tempfile.TemporaryDirectory(prefix="v61-refill-") as folder:
                local_video = Path(folder) / "source.mp4"
                sha256 = download_video(cj_product["video"], local_video)
                if args.artifact_dir:
                    shutil.copy2(local_video, args.artifact_dir / drive_name)
                    drive_id = "pending-user-drive-upload"
                else:
                    drive_id = upload_drive(local_video, drive_name, folder_id)
            item = build_queue_item(
                product, cj_product, match, drive_id, drive_name, sha256, now
            )
            if args.artifact_dir:
                item["status"] = "pending_drive"
            queue.setdefault("items", []).insert(0, item)
            used_product_ids.add(product["productId"])
            used_cj_ids.add(cj_product["id"])
            added += 1
            print(
                f"Queued V61 product {product['productId']} {product['productName']} "
                f"(score={score}, match={match['confidence']:.2f})"
            )
        except Exception as exc:
            print(f"Candidate {cj_product['id']} skipped safely: {exc}")

    if added:
        if args.artifact_dir:
            (args.artifact_dir / "queue_items.json").write_text(
                json.dumps({"items": queue.get("items", [])[:added]}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        else:
            QUEUE_PATH.write_text(
                json.dumps(queue, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    STATUS_PATH.write_text(
        json.dumps({
            "status": "ok",
            "checkedAt": now,
            "readyBefore": ready_count,
            "eligibleCjCandidates": len(cj_rows),
            "added": added,
            "readyAfter": ready_count + added,
            "reason": "queue-refilled" if added else "no-exact-approved-match",
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"V61 refill complete: added={added}, ready_after={ready_count + added}")


if __name__ == "__main__":
    main()
