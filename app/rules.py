from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import Job, JobRuleEvaluation, RuleOutcome

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = PROJECT_ROOT / "config" / "rule_profiles"
DEFAULT_PROFILE_ID = "early_career_strategy_commercial_growth_ops"
DEFAULT_PROFILE_VERSION = "2026-07-13.2"

FieldName = Literal["title", "description"]
MatchType = Literal["phrase", "regex"]
EvidenceKind = Literal["positive", "penalty", "exclusion"]

SUPPORTED_FIELDS = {"title", "description"}
SUPPORTED_MATCH_TYPES = {"phrase", "regex"}
SUPPORTED_CATEGORIES = {"target_title", "positive", "seniority", "technical"}
PROFILE_KEY_SEPARATOR = "::"
MAX_PATTERNS_PER_RULE = 20
MAX_REGEX_PATTERNS_PER_RULE = 4
MAX_PATTERN_LENGTH = 180
REGEX_RULE_REQUIREMENTS = {
    "seniority_title": ("seniority", ("title",)),
    "seniority_description": ("seniority", ("description",)),
    "years_experience": ("seniority", ("description",)),
    "technical_mandatory": ("technical", ("description",)),
}
VALID_OUTCOMES = {item.value for item in RuleOutcome}
OUTCOME_RANK = {
    RuleOutcome.EXCLUDE.value: 0,
    RuleOutcome.WEAK_MATCH.value: 1,
    RuleOutcome.REVIEW.value: 2,
    RuleOutcome.STRONG_MATCH.value: 3,
}
VALID_RECOMMENDATIONS = VALID_OUTCOMES


class ProfileValidationError(ValueError):
    def __init__(self, file_path: Path, message: str) -> None:
        self.file_path = file_path
        super().__init__(f"{file_path}: {message}")


class ProfileSelectionError(ValueError):
    pass


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


@dataclass(frozen=True)
class RuleProfile:
    id: str
    name: str
    description: str
    version: str
    base_score: int
    thresholds: dict[str, int]
    max_description_positive_contribution: int
    max_description_positive_rules: int
    title_seniority_outcome_ceilings: dict[str, str]
    rules: tuple[Rule, ...]
    fingerprint: str
    source_path: Path
    is_default: bool = False

    @property
    def label(self) -> str:
        suffix = " default" if self.is_default else ""
        return f"{self.name} ({self.id} {self.version}{suffix})"


@dataclass(frozen=True)
class ProfileRegistry:
    profiles: tuple[RuleProfile, ...]
    errors: tuple[str, ...]
    duplicate_errors: tuple[str, ...]

    @property
    def valid_profiles(self) -> tuple[RuleProfile, ...]:
        return self.profiles

    @property
    def default_profile(self) -> RuleProfile | None:
        return next((profile for profile in self.profiles if profile.is_default), None)

    def get(self, profile_id: str, version: str) -> RuleProfile:
        for profile in self.profiles:
            if profile.id == profile_id and profile.version == version:
                return profile
        raise ProfileSelectionError(f"Requested rule profile not found: {profile_id} {version}")


def profile_key(profile: RuleProfile) -> str:
    return f"{profile.id}{PROFILE_KEY_SEPARATOR}{profile.version}"


def parse_profile_key(value: str | None) -> tuple[str, str]:
    if value is None or not value.strip():
        raise ProfileSelectionError("profile_key is required")
    parts = value.strip().split(PROFILE_KEY_SEPARATOR)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise ProfileSelectionError("profile_key must contain one profile ID/version pair")
    return parts[0].strip(), parts[1].strip()


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


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _profile_fingerprint(profile_data: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(profile_data).encode("utf-8")).hexdigest()


def _require_keys(file_path: Path, data: dict[str, Any], required: set[str], context: str) -> None:
    missing = sorted(required - set(data))
    if missing:
        raise ProfileValidationError(file_path, f"{context} missing required field(s): {missing}")
    unknown = sorted(set(data) - required)
    if unknown:
        raise ProfileValidationError(file_path, f"{context} contains unknown field(s): {unknown}")


def _require_non_empty_string(file_path: Path, value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileValidationError(file_path, f"{field} must be a non-empty string")
    return value.strip()


def _require_int(file_path: Path, value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProfileValidationError(file_path, f"{field} must be an integer")
    if value < minimum or value > maximum:
        raise ProfileValidationError(file_path, f"{field} must be between {minimum} and {maximum}")
    return value


def _validate_thresholds(file_path: Path, value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ProfileValidationError(file_path, "thresholds must be an object")
    _require_keys(
        file_path,
        value,
        {"weak_match", "review", "strong_match"},
        "thresholds",
    )
    thresholds = {
        name: _require_int(file_path, raw, f"thresholds.{name}", minimum=0, maximum=100)
        for name, raw in value.items()
    }
    if not (thresholds["weak_match"] < thresholds["review"] < thresholds["strong_match"]):
        raise ProfileValidationError(
            file_path,
            "thresholds must be ordered weak_match < review < strong_match",
        )
    return thresholds


def _validate_regex_rule_scope(
    file_path: Path, rule_id: str, category: str, fields: tuple[str, ...]
) -> None:
    expected = REGEX_RULE_REQUIREMENTS.get(rule_id)
    if expected is None:
        raise ProfileValidationError(
            file_path,
            f"rule {rule_id} is not allowed to use regex; use phrase matching instead",
        )
    expected_category, expected_fields = expected
    if category != expected_category or tuple(fields) != expected_fields:
        raise ProfileValidationError(
            file_path,
            f"rule {rule_id} regex scope must be category {expected_category} "
            f"and fields {list(expected_fields)}",
        )


def _has_nested_or_catastrophic_quantifier(pattern: str) -> bool:
    if re.search(r"\([^)]*[*+][^)]*\)\s*(?:[*+]|\{\d+(?:,\d*)?\})", pattern):
        return True
    if re.search(r"\(([^)]*\|[^)]*)\)\s*(?:[*+]|\{\d+(?:,\d*)?\})", pattern):
        return True
    if re.search(r"(?:\.\*|\.\+)(?!\?)", pattern):
        return True
    if re.search(r"\.\{\d+,\}", pattern):
        return True
    if re.search(r"\.\{(?:[9][1-9]|[1-9]\d{2,}),", pattern):
        return True
    return bool(
        re.search(
            r"(?<!\\)(?:[*+?]|\{\d+(?:,\d*)?\})\s*"
            r"(?<!\\)(?:[*+?]|\{\d+(?:,\d*)?\})",
            pattern,
        )
    )


def _validate_regex_pattern(file_path: Path, pattern: str, rule_id: str) -> None:
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise ProfileValidationError(file_path, f"rule {rule_id} regex pattern is too long")
    forbidden = ["(?=", "(?!", "(?<=", "(?<!", "(?P=", "\\g", "*+", "++", "?+"]
    if any(token in pattern for token in forbidden):
        raise ProfileValidationError(
            file_path,
            f"rule {rule_id} regex pattern uses unsupported constructs",
        )
    if re.search(r"(?<!\\)\\[1-9]", pattern) or _has_nested_or_catastrophic_quantifier(pattern):
        raise ProfileValidationError(
            file_path,
            f"rule {rule_id} regex pattern uses unsupported constructs",
        )
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ProfileValidationError(
            file_path, f"rule {rule_id} regex pattern is invalid: {exc}"
        ) from exc


def _validate_rule(file_path: Path, raw_rule: Any, index: int) -> Rule:
    if not isinstance(raw_rule, dict):
        raise ProfileValidationError(file_path, f"rules[{index}] must be an object")
    _require_keys(
        file_path,
        raw_rule,
        {
            "id",
            "label",
            "category",
            "fields",
            "match_type",
            "patterns",
            "weight",
            "hard_exclusion",
            "evidence_label",
        },
        f"rules[{index}]",
    )
    rule_id = _require_non_empty_string(file_path, raw_rule["id"], f"rules[{index}].id")
    label = _require_non_empty_string(file_path, raw_rule["label"], f"rule {rule_id}.label")
    category = _require_non_empty_string(
        file_path, raw_rule["category"], f"rule {rule_id}.category"
    )
    if category not in SUPPORTED_CATEGORIES:
        raise ProfileValidationError(file_path, f"rule {rule_id} has unsupported category")
    fields = raw_rule["fields"]
    if not isinstance(fields, list) or not fields:
        raise ProfileValidationError(file_path, f"rule {rule_id}.fields must be a non-empty list")
    if any(field not in SUPPORTED_FIELDS for field in fields):
        raise ProfileValidationError(file_path, f"rule {rule_id} has unsupported field")
    match_type = _require_non_empty_string(
        file_path, raw_rule["match_type"], f"rule {rule_id}.match_type"
    )
    if match_type not in SUPPORTED_MATCH_TYPES:
        raise ProfileValidationError(file_path, f"rule {rule_id} has unsupported match_type")
    patterns = raw_rule["patterns"]
    if not isinstance(patterns, list) or not patterns:
        raise ProfileValidationError(file_path, f"rule {rule_id}.patterns must be a non-empty list")
    if len(patterns) > MAX_PATTERNS_PER_RULE:
        raise ProfileValidationError(
            file_path,
            f"rule {rule_id}.patterns must contain at most {MAX_PATTERNS_PER_RULE} pattern(s)",
        )
    clean_patterns = tuple(
        _require_non_empty_string(file_path, pattern, f"rule {rule_id}.patterns")
        for pattern in patterns
    )
    for pattern in clean_patterns:
        if len(pattern) > MAX_PATTERN_LENGTH:
            raise ProfileValidationError(file_path, f"rule {rule_id} pattern is too long")
    if match_type == "regex":
        _validate_regex_rule_scope(file_path, rule_id, category, tuple(fields))
        if len(clean_patterns) > MAX_REGEX_PATTERNS_PER_RULE:
            raise ProfileValidationError(
                file_path,
                f"rule {rule_id}.patterns must contain at most "
                f"{MAX_REGEX_PATTERNS_PER_RULE} regex pattern(s)",
            )
        for pattern in clean_patterns:
            _validate_regex_pattern(file_path, pattern, rule_id)
    weight = _require_int(
        file_path, raw_rule["weight"], f"rule {rule_id}.weight", minimum=-100, maximum=100
    )
    hard_exclusion = raw_rule["hard_exclusion"]
    if not isinstance(hard_exclusion, bool):
        raise ProfileValidationError(file_path, f"rule {rule_id}.hard_exclusion must be boolean")
    evidence_label = _require_non_empty_string(
        file_path, raw_rule["evidence_label"], f"rule {rule_id}.evidence_label"
    )
    if hard_exclusion and not evidence_label:
        raise ProfileValidationError(
            file_path, f"rule {rule_id} hard exclusion needs evidence_label"
        )
    return Rule(
        id=rule_id,
        label=label,
        category=category,
        fields=tuple(fields),
        match_type=match_type,
        patterns=clean_patterns,
        weight=weight,
        evidence_label=evidence_label,
        hard_exclusion=hard_exclusion,
    )


def validate_profile_data(data: Any, file_path: Path) -> RuleProfile:
    if not isinstance(data, dict):
        raise ProfileValidationError(file_path, "profile must be a JSON object")
    required = {
        "id",
        "name",
        "description",
        "version",
        "default",
        "base_score",
        "thresholds",
        "max_description_positive_contribution",
        "max_description_positive_rules",
        "title_seniority_outcome_ceilings",
        "rules",
    }
    _require_keys(file_path, data, required, "profile")
    profile_id = _require_non_empty_string(file_path, data["id"], "id")
    version = _require_non_empty_string(file_path, data["version"], "version")
    name = _require_non_empty_string(file_path, data["name"], "name")
    description = _require_non_empty_string(file_path, data["description"], "description")
    if not isinstance(data["default"], bool):
        raise ProfileValidationError(file_path, "default must be boolean")
    base_score = _require_int(file_path, data["base_score"], "base_score", minimum=0, maximum=100)
    thresholds = _validate_thresholds(file_path, data["thresholds"])
    max_description_contribution = _require_int(
        file_path,
        data["max_description_positive_contribution"],
        "max_description_positive_contribution",
        minimum=0,
        maximum=100,
    )
    max_description_rules = _require_int(
        file_path,
        data["max_description_positive_rules"],
        "max_description_positive_rules",
        minimum=0,
        maximum=50,
    )
    ceilings = data["title_seniority_outcome_ceilings"]
    if not isinstance(ceilings, dict):
        raise ProfileValidationError(file_path, "title_seniority_outcome_ceilings must be object")
    clean_ceilings: dict[str, str] = {}
    for signal, outcome in ceilings.items():
        signal_text = _require_non_empty_string(
            file_path, signal, "title_seniority_outcome_ceilings signal"
        )
        if outcome not in VALID_OUTCOMES - {RuleOutcome.EXCLUDE.value}:
            raise ProfileValidationError(
                file_path,
                f"title_seniority_outcome_ceilings.{signal_text} has invalid outcome",
            )
        clean_ceilings[normalize_text(signal_text)] = outcome
    raw_rules = data["rules"]
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ProfileValidationError(file_path, "rules must be a non-empty list")
    rules = tuple(_validate_rule(file_path, rule, index) for index, rule in enumerate(raw_rules))
    rule_ids = [rule.id for rule in rules]
    duplicates = sorted({rule_id for rule_id in rule_ids if rule_ids.count(rule_id) > 1})
    if duplicates:
        raise ProfileValidationError(file_path, f"duplicate rule IDs: {duplicates}")
    fingerprint = _profile_fingerprint(data)
    return RuleProfile(
        id=profile_id,
        name=name,
        description=description,
        version=version,
        base_score=base_score,
        thresholds=thresholds,
        max_description_positive_contribution=max_description_contribution,
        max_description_positive_rules=max_description_rules,
        title_seniority_outcome_ceilings=clean_ceilings,
        rules=rules,
        fingerprint=fingerprint,
        source_path=file_path,
        is_default=data["default"],
    )


def load_profile_file(file_path: Path) -> RuleProfile:
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProfileValidationError(file_path, f"malformed JSON: {exc}") from exc
    return validate_profile_data(data, file_path)


def load_profile_registry(profile_dir: Path = PROFILE_DIR) -> ProfileRegistry:
    profile_dir = profile_dir.resolve()
    if not profile_dir.exists():
        return ProfileRegistry((), (f"{profile_dir}: profile directory does not exist",), ())
    profiles: list[RuleProfile] = []
    errors: list[str] = []
    for file_path in sorted(profile_dir.glob("*.json"), key=lambda path: path.name.lower()):
        try:
            profiles.append(load_profile_file(file_path.resolve()))
        except ProfileValidationError as exc:
            errors.append(str(exc))
    seen: dict[tuple[str, str], Path] = {}
    duplicates: list[str] = []
    unique_profiles: list[RuleProfile] = []
    for profile in sorted(profiles, key=lambda item: (item.id, item.version, item.name)):
        key = (profile.id, profile.version)
        if key in seen:
            duplicates.append(
                f"duplicate profile ID/version {profile.id} {profile.version}: "
                f"{seen[key]} and {profile.source_path}"
            )
            continue
        seen[key] = profile.source_path
        unique_profiles.append(profile)
    default_matches = [
        profile
        for profile in unique_profiles
        if profile.id == DEFAULT_PROFILE_ID and profile.version == DEFAULT_PROFILE_VERSION
    ]
    if len(default_matches) != 1:
        errors.append(
            f"default profile unavailable: {DEFAULT_PROFILE_ID} {DEFAULT_PROFILE_VERSION}"
        )
    return ProfileRegistry(tuple(unique_profiles), tuple(errors), tuple(duplicates))


def get_default_profile() -> RuleProfile:
    registry = load_profile_registry()
    profile = registry.default_profile
    if profile is None:
        raise ProfileSelectionError(
            f"No valid default profile available: {DEFAULT_PROFILE_ID} {DEFAULT_PROFILE_VERSION}"
        )
    return profile


def get_profile_or_default(
    profile_id: str | None = None, version: str | None = None
) -> RuleProfile:
    registry = load_profile_registry()
    if profile_id or version:
        if not profile_id or not version:
            raise ProfileSelectionError("Both profile_id and profile_version are required")
        return registry.get(profile_id, version)
    return get_default_profile()


def get_profile_by_key(profile_key_value: str | None) -> RuleProfile:
    profile_id, version = parse_profile_key(profile_key_value)
    return load_profile_registry().get(profile_id, version)


PROFILE_ID = DEFAULT_PROFILE_ID
PROFILE_VERSION = DEFAULT_PROFILE_VERSION
BASE_SCORE = 50
STRONG_MATCH_THRESHOLD = 75
REVIEW_THRESHOLD = 50
WEAK_MATCH_THRESHOLD = 30
MAX_DESCRIPTION_POSITIVE_CONTRIBUTION = 20
MAX_DESCRIPTION_POSITIVE_RULES = 3
TITLE_SENIORITY_OUTCOME_CEILINGS = {
    "senior": RuleOutcome.REVIEW.value,
    "lead": RuleOutcome.REVIEW.value,
    "manager": RuleOutcome.WEAK_MATCH.value,
    "principal": RuleOutcome.WEAK_MATCH.value,
    "head of": RuleOutcome.WEAK_MATCH.value,
    "director": RuleOutcome.WEAK_MATCH.value,
}
ALL_RULES: tuple[Rule, ...] = ()


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


def _apply_description_positive_cap(positives: list[dict], profile: RuleProfile) -> list[dict]:
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
        id(item) for item in ranked_description_items[: profile.max_description_positive_rules]
    }
    remaining = profile.max_description_positive_contribution
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


def _score_based_outcome(score: int, profile: RuleProfile) -> str:
    if score >= profile.thresholds["strong_match"]:
        return RuleOutcome.STRONG_MATCH.value
    if score >= profile.thresholds["review"]:
        return RuleOutcome.REVIEW.value
    if score >= profile.thresholds["weak_match"]:
        return RuleOutcome.WEAK_MATCH.value
    return RuleOutcome.EXCLUDE.value


def _title_seniority_ceiling(penalties: list[dict], profile: RuleProfile) -> dict | None:
    ceilings = []
    for item in penalties:
        if item["rule_id"] != "seniority_title":
            continue
        matched_text = normalize_text(item["matched_text"])
        ceiling = profile.title_seniority_outcome_ceilings.get(matched_text)
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


def evaluate_job_content(job: Job, profile: RuleProfile | None = None) -> dict:
    profile = profile or get_default_profile()
    title = normalize_text(job.title)
    description = normalize_text(job.description)
    field_text = {"title": title, "description": description}
    positives: list[dict] = []
    penalties: list[dict] = []
    exclusions: list[dict] = []

    for rule in profile.rules:
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
    positives = _apply_description_positive_cap(positives, profile)
    penalties = sorted(
        _unique_by_rule(penalties), key=lambda item: (item["weight"], item["rule_id"])
    )
    exclusions = sorted(
        _unique_by_rule(exclusions), key=lambda item: (item["rule_id"], item["field"])
    )
    score = profile.base_score + sum(
        item["effective_weight"] for item in positives + penalties + exclusions
    )
    score = max(0, min(100, score))
    score_based_outcome = _score_based_outcome(score, profile)
    outcome_ceiling = _title_seniority_ceiling(penalties, profile)
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
        "profile_id": profile.id,
        "profile_name": profile.name,
        "profile_version": profile.version,
        "profile_fingerprint": profile.fingerprint,
        "content_fingerprint": content_fingerprint(job),
    }


def latest_evaluation(
    db: Session,
    job_id: int,
    profile: RuleProfile | None = None,
) -> JobRuleEvaluation | None:
    query = select(JobRuleEvaluation).where(JobRuleEvaluation.job_id == job_id)
    if profile is not None:
        query = query.where(
            JobRuleEvaluation.profile_id == profile.id,
            JobRuleEvaluation.profile_version == profile.version,
        )
    return db.scalar(
        query.order_by(desc(JobRuleEvaluation.evaluated_at), desc(JobRuleEvaluation.id)).limit(1)
    )


def evaluation_state(
    job: Job,
    evaluation: JobRuleEvaluation | None,
    profile: RuleProfile | None = None,
) -> str:
    profile = profile or get_default_profile()
    if evaluation is None:
        return "unevaluated"
    if (
        evaluation.profile_id != profile.id
        or evaluation.profile_version != profile.version
        or (
            evaluation.profile_fingerprint is not None
            and evaluation.profile_fingerprint != profile.fingerprint
        )
        or evaluation.content_fingerprint != content_fingerprint(job)
    ):
        return "stale"
    return "evaluated"


def effective_recommendation(evaluation: JobRuleEvaluation | None) -> str | None:
    if evaluation is None:
        return None
    return evaluation.recommendation_override or evaluation.outcome


def _assert_profile_version_integrity(
    db: Session, profile: RuleProfile, current: JobRuleEvaluation | None
) -> None:
    conflicting = db.scalar(
        select(JobRuleEvaluation)
        .where(
            JobRuleEvaluation.profile_id == profile.id,
            JobRuleEvaluation.profile_version == profile.version,
            JobRuleEvaluation.profile_fingerprint.is_not(None),
            JobRuleEvaluation.profile_fingerprint != profile.fingerprint,
        )
        .limit(1)
    )
    if conflicting is not None:
        raise ProfileSelectionError(
            f"Profile {profile.id} {profile.version} content changed. "
            "Increment the profile version before evaluating."
        )
    if (
        current is not None
        and current.profile_fingerprint is not None
        and current.profile_fingerprint != profile.fingerprint
    ):
        raise ProfileSelectionError(
            f"Profile {profile.id} {profile.version} fingerprint conflicts with latest evaluation."
        )


def evaluate_and_store_job(
    db: Session,
    job: Job,
    profile: RuleProfile | None = None,
) -> JobRuleEvaluation:
    profile = profile or get_default_profile()
    current = latest_evaluation(db, job.id, profile)
    _assert_profile_version_integrity(db, profile, current)
    result = evaluate_job_content(job, profile)
    if (
        current is not None
        and current.profile_id == result["profile_id"]
        and current.profile_version == result["profile_version"]
        and current.profile_fingerprint == result["profile_fingerprint"]
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
        profile_fingerprint=result["profile_fingerprint"],
        evaluated_at=datetime.now(UTC),
        recommendation_override=current.recommendation_override if current else None,
        content_fingerprint=result["content_fingerprint"],
    )
    db.add(evaluation)
    db.commit()
    db.refresh(evaluation)
    return evaluation


def set_recommendation_override(
    db: Session, job: Job, override: str | None, profile: RuleProfile | None = None
) -> JobRuleEvaluation:
    if override is not None and override not in VALID_RECOMMENDATIONS:
        raise ValueError("Invalid recommendation override")
    profile = profile or get_default_profile()
    evaluation = latest_evaluation(db, job.id, profile) or evaluate_and_store_job(db, job, profile)
    evaluation.recommendation_override = override
    db.commit()
    db.refresh(evaluation)
    return evaluation
