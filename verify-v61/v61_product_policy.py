from __future__ import annotations

from datetime import datetime, timezone


INTERNAL_FACT_MARKERS = (
    "api", "상품 id", "productid", "동일상품", "동일 상품",
    "제휴링크", "제휴 링크", "검증 정보", "내부 지시",
)


def clean(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def parse_time(value: object) -> datetime | None:
    text = clean(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def usable_facts(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        fact = clean(value)
        lowered = fact.lower()
        if fact and not any(marker in lowered for marker in INTERNAL_FACT_MARKERS):
            result.append(fact)
    return result


def source_candidates(item: dict) -> list[dict]:
    rows = item.get("sources")
    if not isinstance(rows, list) or not rows:
        rows = [item.get("source")]
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("exactProductVerified") is not True:
            continue
        if row.get("rightsApproved") is not True:
            continue
        result.append(row)
    return sorted(
        result,
        key=lambda row: (
            int(row.get("priority") or 999),
            clean(row.get("sourcePlatform")),
        ),
    )


def evaluate_item(item: dict, config: dict, now: datetime | None = None) -> tuple[bool, int, str]:
    policy = config.get("productDiscovery") or {}
    product = item.get("product") or {}
    name = clean(product.get("productName")).lower()
    blocked = [clean(x).lower() for x in policy.get("blockedKeywords") or [] if clean(x)]
    if any(keyword in name for keyword in blocked):
        return False, 0, "blocked-saturated-product"

    discovery = item.get("discovery")
    if not isinstance(discovery, dict):
        return False, 0, "missing-discovery-evidence"

    discovered_at = parse_time(discovery.get("discoveredAt"))
    if discovered_at is None:
        return False, 0, "missing-discovery-time"
    current = now or datetime.now(timezone.utc)
    age_days = max(0, (current - discovered_at).days)
    max_age_days = int(policy.get("maximumCandidateAgeDays") or 90)
    if age_days > max_age_days:
        return False, 0, "stale-candidate"

    facts = usable_facts(item.get("verifiedFacts"))
    if len(facts) < int(policy.get("minimumConsumerFacts") or 2):
        return False, 0, "insufficient-consumer-facts"

    sources = source_candidates(item)
    if len(sources) < int(policy.get("minimumApprovedSources") or 1):
        return False, 0, "no-approved-source"

    trend_score = max(0, min(40, int(discovery.get("trendScore") or 0)))
    novelty_score = max(0, min(30, int(discovery.get("noveltyScore") or 0)))
    demo_score = max(0, min(20, int(discovery.get("demonstrationScore") or 0)))
    diversity_score = min(10, max(0, (len(sources) - 1) * 5))
    recency_bonus = max(0, 10 - age_days // 9)
    total = min(100, trend_score + novelty_score + demo_score + diversity_score + recency_bonus)
    minimum = int(policy.get("minimumSelectionScore") or 65)
    if total < minimum:
        return False, total, "selection-score-too-low"
    return True, total, "approved"

