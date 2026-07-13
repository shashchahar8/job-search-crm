from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import Job, JobRuleEvaluation, RuleOutcome

PROFILE_ID = "early_career_strategy_commercial_growth_ops"
PROFILE_VERSION = "2026-07-13.2"
BASE_SCORE = 50
STRONG_MATCH_THRESHOLD = 75
REVIEW_THRESHOLD = 50
WEAK_MATCH_THRESHOLD = 30
MAX_DESCRIPTION_POSITIVE_CONTRIBUTION = 20
MAX_DESCRIPTION_POSITIVE_RULES = 3
OUTCOME_RANK = {
    RuleOutcome.EXCLUDE.value: 0,
    RuleOutcome.WEAK_MATCH.value: 1,
    RuleOutcome.REVIEW.value: 2,
    RuleOutcome.STRONG_MATCH.value: 3,
}
TITLE_SENIORITY_OUTCOME_CEILINGS = {
    "senior": RuleOutcome.REVIEW.value,
    "lead": RuleOutcome.REVIEW.value,
    "manager": RuleOutcome.WEAK_MATCH.value,
    "principal": RuleOutcome.WEAK_MATCH.value,
    "head of": RuleOutcome.WEAK_MATCH.value,
    "director": RuleOutcome.WEAK_MATCH.value,
}

FieldName = Literal["title", "description"]
MatchType = Literal["phrase", "regex"]
EvidenceKind = Literal["positive", "penalty", "exclusion"]


@dataclass(frozen=True)
class Rule:
    id: str
    label: str
    category: str
    fields: tuple[FieldName, ...]
    match_type: MatchType
    patterns: tuple[str, ...]
    weight: int
    evidence_label: str
    hard_exclusion: bool = False


TARGET_TITLE_RULES = (
    Rule(
        "target_title_strategy_analyst",
        "Strategy Analyst title",
        "target_title",
        ("title",),
        "phrase",
        ("strategy analyst",),
        35,
        "title contains Strategy Analyst",
    ),
    Rule(
        "target_title_commercial_analyst",
        "Commercial Analyst title",
        "target_title",
        ("title",),
        "phrase",
        ("commercial analyst",),
        35,
        "title contains Commercial Analyst",
    ),
    Rule(
        "target_title_growth_analyst",
        "Growth Analyst title",
        "target_title",
        ("title",),
        "phrase",
        ("growth analyst",),
        35,
        "title contains Growth Analyst",
    ),
    Rule(
        "target_title_operations_analyst",
        "Operations Analyst title",
        "target_title",
        ("title",),
        "phrase",
        ("operations analyst", "operation analyst"),
        35,
        "title contains Operations Analyst",
    ),
    Rule(
        "target_title_business_analyst",
        "Business Analyst title",
        "target_title",
        ("title",),
        "phrase",
        ("business analyst",),
        28,
        "title contains Business Analyst",
    ),
    Rule(
        "target_title_business_performance",
        "Business Performance Analyst title",
        "target_title",
        ("title",),
        "phrase",
        ("business performance analyst",),
        38,
        "title contains Business Performance Analyst",
    ),
    Rule(
        "target_title_corporate_development",
        "Corporate Development Analyst title",
        "target_title",
        ("title",),
        "phrase",
        ("corporate development analyst",),
        38,
        "title contains Corporate Development Analyst",
    ),
    Rule(
        "target_title_strategic_projects",
        "Strategic Projects Analyst title",
        "target_title",
        ("title",),
        "phrase",
        ("strategic projects analyst", "strategic project analyst"),
        38,
        "title contains Strategic Projects Analyst",
    ),
)

POSITIVE_RULES = (
    Rule(
        "positive_strategy",
        "Strategy",
        "positive",
        ("title", "description"),
        "phrase",
        ("strategy", "strategic"),
        14,
        "mentions strategy",
    ),
    Rule(
        "positive_commercial",
        "Commercial analysis",
        "positive",
        ("title", "description"),
        "phrase",
        ("commercial analysis", "commercial analyst", "commercial insights"),
        14,
        "mentions commercial analysis",
    ),
    Rule(
        "positive_business_performance",
        "Business performance",
        "positive",
        ("title", "description"),
        "phrase",
        ("business performance",),
        14,
        "mentions business performance",
    ),
    Rule(
        "positive_market_research",
        "Market research",
        "positive",
        ("title", "description"),
        "phrase",
        ("market research",),
        12,
        "mentions market research",
    ),
    Rule(
        "positive_insights",
        "Insights",
        "positive",
        ("title", "description"),
        "phrase",
        ("insights",),
        10,
        "mentions insights",
    ),
    Rule(
        "positive_financial_modelling",
        "Financial modelling",
        "positive",
        ("title", "description"),
        "phrase",
        ("financial modelling", "financial modeling"),
        12,
        "mentions financial modelling",
    ),
    Rule(
        "positive_growth",
        "Growth",
        "positive",
        ("title", "description"),
        "phrase",
        ("growth",),
        12,
        "mentions growth",
    ),
    Rule(
        "positive_transformation",
        "Transformation",
        "positive",
        ("title", "description"),
        "phrase",
        ("transformation",),
        10,
        "mentions transformation",
    ),
    Rule(
        "positive_operational_improvement",
        "Operational improvement",
        "positive",
        ("title", "description"),
        "phrase",
        ("operational improvement", "process improvement"),
        12,
        "mentions operational improvement",
    ),
    Rule(
        "positive_stakeholder",
        "Stakeholder management",
        "positive",
        ("description",),
        "phrase",
        ("stakeholder management", "stakeholder engagement"),
        7,
        "mentions stakeholder management",
    ),
    Rule(
        "positive_executive_reporting",
        "Executive reporting",
        "positive",
        ("description",),
        "phrase",
        ("executive reporting", "executive reports"),
        7,
        "mentions executive reporting",
    ),
    Rule(
        "positive_early_career",
        "Early-career scope",
        "positive",
        ("title", "description"),
        "phrase",
        (
            "graduate",
            "junior",
            "entry level",
            "entry-level",
            "early career",
            "early-career",
            "associate",
        ),
        12,
        "mentions early-career scope",
    ),
)

PENALTY_RULES = (
    Rule(
        "seniority_title",
        "Title seniority signal",
        "seniority",
        ("title",),
        "regex",
        (r"\b(senior|lead|manager|principal|director)\b", r"\bhead\s+of\b"),
        -30,
        "title contains a seniority signal",
    ),
    Rule(
        "seniority_description",
        "Description seniority signal",
        "seniority",
        ("description",),
        "regex",
        (r"\bsenior\s+(role|position|level)\b", r"\bmanager\s+level\b"),
        -8,
        "description contains a seniority signal",
    ),
    Rule(
        "years_experience",
        "Substantial experience requirement",
        "seniority",
        ("description",),
        "regex",
        (r"\b([6-9]|[1-9][0-9])\+?\s+years(?:'| of)?\s+experience\b",),
        -22,
        "advertisement states a substantial years-of-experience requirement",
    ),
    Rule(
        "technical_mandatory",
        "Mandatory technical requirement",
        "technical",
        ("description",),
        "regex",
        (
            r"\bmust\s+have\b.{0,80}\b(sql|python|java|aws|azure|tableau|power\s*bi)\b",
            r"\brequired\b.{0,80}\b(sql|python|java|aws|azure|tableau|power\s*bi)\b",
        ),
        -25,
        "advertisement mentions a configured mandatory technical requirement",
    ),
)

EXCLUSION_RULES = (
    Rule(
        "technical_title_business_systems",
        "Technical or systems analyst title",
        "technical",
        ("title",),
        "phrase",
        ("technical business analyst", "systems analyst", "system analyst"),
        -45,
        "title contains a configured technical analyst phrase",
        True,
    ),
    Rule(
        "technical_title_engineering",
        "Engineering title",
        "technical",
        ("title",),
        "phrase",
        (
            "software engineer",
            "software engineering",
            "infrastructure engineer",
            "infrastructure engineering",
            "data engineer",
            "data engineering",
        ),
        -45,
        "title contains a configured engineering phrase",
        True,
    ),
)

ALL_RULES = TARGET_TITLE_RULES + POSITIVE_RULES + PENALTY_RULES + EXCLUSION_RULES
VALID_RECOMMENDATIONS = {item.value for item in RuleOutcome}


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    normalized = normalized.lower()
    normalized = normalized.replace("&", " and ")
    normalized = re.sub(r"[\u2018\u2019']", "", normalized)
    normalized = re.sub(r"[-_/]", " ", normalized)
    normalized = re.sub(r"[^a-z0-9+.\s]", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def content_fingerprint(job: Job) -> str:
    body = "\n".join(
        [
            normalize_text(job.title),
            normalize_text(job.company),
            normalize_text(job.location),
            normalize_text(job.description),
        ]
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _phrase_regex(phrase: str) -> re.Pattern[str]:
    words = [re.escape(part) for part in normalize_text(phrase).split()]
    return re.compile(r"(?<![a-z0-9])" + r"\s+".join(words) + r"(?![a-z0-9])")


def _matches(rule: Rule, field: FieldName, text: str) -> str | None:
    if not text:
        return None
    if rule.id == "seniority_description" and "senior stakeholders" in text:
        return None
    for pattern in rule.patterns:
        if rule.match_type == "phrase":
            match = _phrase_regex(pattern).search(text)
        else:
            match = re.search(pattern, text)
        if match:
            return match.group(0)
    return None


def _evidence(rule: Rule, field: FieldName, matched_text: str, kind: EvidenceKind) -> dict:
    weight = rule.weight
    if kind == "positive" and field == "description" and rule.category != "target_title":
        weight = max(1, round(weight * 0.5))
    return {
        "rule_id": rule.id,
        "label": rule.label,
        "category": rule.category,
        "field": field,
        "matched_text": matched_text,
        "matched_weight": weight,
        "effective_weight": weight,
        "weight": weight,
        "hard_exclusion": rule.hard_exclusion,
        "evidence_label": rule.evidence_label,
    }


def _unique_by_rule(evidence: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    unique = []
    for item in evidence:
        key = (item["rule_id"], item["field"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _outcome(score: int, exclusions: list[dict]) -> str:
    if exclusions:
        return RuleOutcome.EXCLUDE.value
    if score >= STRONG_MATCH_THRESHOLD:
        return RuleOutcome.STRONG_MATCH.value
    if score >= REVIEW_THRESHOLD:
        return RuleOutcome.REVIEW.value
    if score >= WEAK_MATCH_THRESHOLD:
        return RuleOutcome.WEAK_MATCH.value
    return RuleOutcome.EXCLUDE.value


def _apply_description_positive_cap(positives: list[dict]) -> list[dict]:
    description_items = [
        item
        for item in positives
        if item["field"] == "description" and item["effective_weight"] > 0
    ]
    ranked_description_items = sorted(
        description_items,
        key=lambda item: (-item["matched_weight"], item["rule_id"]),
    )
    contributing_ids = {
        id(item) for item in ranked_description_items[:MAX_DESCRIPTION_POSITIVE_RULES]
    }
    remaining = MAX_DESCRIPTION_POSITIVE_CONTRIBUTION
    for item in ranked_description_items:
        if id(item) not in contributing_ids or remaining <= 0:
            item["effective_weight"] = 0
            item["weight"] = 0
            item["score_note"] = "Description positive cap reached."
            continue
        contribution = min(item["matched_weight"], remaining)
        item["effective_weight"] = contribution
        item["weight"] = contribution
        remaining -= contribution
        if contribution < item["matched_weight"]:
            item["score_note"] = "Partially limited by description positive cap."
    return positives


def _score_based_outcome(score: int) -> str:
    if score >= STRONG_MATCH_THRESHOLD:
        return RuleOutcome.STRONG_MATCH.value
    if score >= REVIEW_THRESHOLD:
        return RuleOutcome.REVIEW.value
    if score >= WEAK_MATCH_THRESHOLD:
        return RuleOutcome.WEAK_MATCH.value
    return RuleOutcome.EXCLUDE.value


def _title_seniority_ceiling(penalties: list[dict]) -> dict | None:
    ceilings = []
    for item in penalties:
        if item["rule_id"] != "seniority_title":
            continue
        matched_text = normalize_text(item["matched_text"])
        ceiling = TITLE_SENIORITY_OUTCOME_CEILINGS.get(matched_text)
        if ceiling is None:
            continue
        ceilings.append(
            {
                "signal": matched_text,
                "outcome_ceiling": ceiling,
                "evidence_label": item["evidence_label"],
                "rule_id": item["rule_id"],
            }
        )
        item["outcome_ceiling"] = ceiling
    if not ceilings:
        return None
    return min(ceilings, key=lambda item: OUTCOME_RANK[item["outcome_ceiling"]])


def _apply_outcome_ceiling(outcome: str, ceiling: dict | None) -> str:
    if ceiling is None:
        return outcome
    if OUTCOME_RANK[outcome] > OUTCOME_RANK[ceiling["outcome_ceiling"]]:
        return ceiling["outcome_ceiling"]
    return outcome


def _explanation(
    score: int,
    outcome: str,
    positives: list[dict],
    penalties: list[dict],
    exclusions: list[dict],
    *,
    score_based_outcome: str,
    outcome_ceiling: dict | None,
) -> str:
    readable = outcome.replace("_", " ")
    if not positives and not penalties and not exclusions:
        return (
            f"Needs review ({score}/100). No strong positive or exclusion signals "
            "were detected by the configured rules."
        )
    parts = [f"Rule recommendation: {readable} ({score}/100)."]
    if positives:
        labels = ", ".join(item["evidence_label"] for item in positives[:3])
        parts.append(f"Positive signals: {labels}.")
    if penalties:
        labels = ", ".join(item["evidence_label"] for item in penalties[:3])
        parts.append(f"Warnings: {labels}.")
    if exclusions:
        labels = ", ".join(item["evidence_label"] for item in exclusions[:2])
        parts.append(
            "A hard-exclusion rule set the final outcome to exclude despite the "
            f"score-based outcome of {score_based_outcome.replace('_', ' ')}: {labels}."
        )
    elif outcome_ceiling and score_based_outcome != outcome:
        parts.append(
            "The role had positive functional evidence, but the early-career profile "
            f"limited the recommendation to {outcome.replace('_', ' ')} because the title "
            f"contains the seniority signal {outcome_ceiling['signal']}."
        )
    elif not penalties:
        parts.append("No significant warning was detected by the configured rules.")
    return " ".join(parts)


def evaluate_job_content(job: Job) -> dict:
    title = normalize_text(job.title)
    description = normalize_text(job.description)
    field_text = {"title": title, "description": description}
    positives: list[dict] = []
    penalties: list[dict] = []
    exclusions: list[dict] = []

    for rule in ALL_RULES:
        for field in rule.fields:
            matched = _matches(rule, field, field_text[field])
            if not matched:
                continue
            if rule.hard_exclusion:
                exclusions.append(_evidence(rule, field, matched, "exclusion"))
            elif rule.weight < 0:
                penalties.append(_evidence(rule, field, matched, "penalty"))
            else:
                positives.append(_evidence(rule, field, matched, "positive"))

    positives = sorted(
        _unique_by_rule(positives), key=lambda item: (-item["weight"], item["rule_id"])
    )
    positives = _apply_description_positive_cap(positives)
    penalties = sorted(
        _unique_by_rule(penalties), key=lambda item: (item["weight"], item["rule_id"])
    )
    exclusions = sorted(
        _unique_by_rule(exclusions), key=lambda item: (item["rule_id"], item["field"])
    )
    score = BASE_SCORE + sum(
        item["effective_weight"] for item in positives + penalties + exclusions
    )
    score = max(0, min(100, score))
    score_based_outcome = _score_based_outcome(score)
    outcome_ceiling = _title_seniority_ceiling(penalties)
    outcome = _apply_outcome_ceiling(score_based_outcome, outcome_ceiling)
    if exclusions:
        outcome = RuleOutcome.EXCLUDE.value
        for item in exclusions:
            item["hard_exclusion_applied"] = True
            item["score_based_outcome"] = score_based_outcome
    return {
        "score": score,
        "outcome": outcome,
        "score_based_outcome": score_based_outcome,
        "outcome_ceiling": outcome_ceiling,
        "hard_exclusion_applied": bool(exclusions),
        "positive_evidence": positives,
        "penalty_evidence": penalties,
        "exclusion_evidence": exclusions,
        "explanation": _explanation(
            score,
            outcome,
            positives,
            penalties,
            exclusions,
            score_based_outcome=score_based_outcome,
            outcome_ceiling=outcome_ceiling,
        ),
        "profile_id": PROFILE_ID,
        "profile_version": PROFILE_VERSION,
        "content_fingerprint": content_fingerprint(job),
    }


def latest_evaluation(db: Session, job_id: int) -> JobRuleEvaluation | None:
    return db.scalar(
        select(JobRuleEvaluation)
        .where(JobRuleEvaluation.job_id == job_id)
        .order_by(desc(JobRuleEvaluation.evaluated_at), desc(JobRuleEvaluation.id))
        .limit(1)
    )


def evaluation_state(job: Job, evaluation: JobRuleEvaluation | None) -> str:
    if evaluation is None:
        return "unevaluated"
    if (
        evaluation.profile_id != PROFILE_ID
        or evaluation.profile_version != PROFILE_VERSION
        or evaluation.content_fingerprint != content_fingerprint(job)
    ):
        return "stale"
    return "evaluated"


def effective_recommendation(evaluation: JobRuleEvaluation | None) -> str | None:
    if evaluation is None:
        return None
    return evaluation.recommendation_override or evaluation.outcome


def evaluate_and_store_job(db: Session, job: Job) -> JobRuleEvaluation:
    current = latest_evaluation(db, job.id)
    result = evaluate_job_content(job)
    if (
        current is not None
        and current.profile_id == result["profile_id"]
        and current.profile_version == result["profile_version"]
        and current.content_fingerprint == result["content_fingerprint"]
    ):
        return current
    evaluation = JobRuleEvaluation(
        job_id=job.id,
        score=result["score"],
        outcome=result["outcome"],
        positive_evidence=result["positive_evidence"],
        penalty_evidence=result["penalty_evidence"],
        exclusion_evidence=result["exclusion_evidence"],
        explanation=result["explanation"],
        profile_id=result["profile_id"],
        profile_version=result["profile_version"],
        evaluated_at=datetime.now(UTC),
        recommendation_override=current.recommendation_override if current else None,
        content_fingerprint=result["content_fingerprint"],
    )
    db.add(evaluation)
    db.commit()
    db.refresh(evaluation)
    return evaluation


def set_recommendation_override(db: Session, job: Job, override: str | None) -> JobRuleEvaluation:
    if override is not None and override not in VALID_RECOMMENDATIONS:
        raise ValueError("Invalid recommendation override")
    evaluation = latest_evaluation(db, job.id) or evaluate_and_store_job(db, job)
    evaluation.recommendation_override = override
    db.commit()
    db.refresh(evaluation)
    return evaluation
