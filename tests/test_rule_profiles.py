import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base, Job, JobRuleEvaluation, RuleOutcome
from app.repository import create_search_and_run, save_job_discovery
from app.rules import (
    ProfileSelectionError,
    ProfileValidationError,
    evaluate_and_store_job,
    evaluate_job_content,
    evaluation_state,
    get_default_profile,
    load_profile_registry,
    validate_profile_data,
)


def _default_data() -> dict:
    return json.loads(Path("config/rule_profiles/default.json").read_text(encoding="utf-8"))


def _write_profile(directory: Path, name: str, data: dict | str) -> Path:
    path = directory / name
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _job_payload(seek_job_id: str, title: str, description: str = "Strategy work."):
    return {
        "seek_job_id": seek_job_id,
        "fallback_key": f"fallback-{seek_job_id}",
        "title": title,
        "company": "Example Co",
        "location": "Sydney NSW",
        "salary": None,
        "work_type": "Full time",
        "posting_date": "1d ago",
        "url": f"https://www.seek.com.au/job/{seek_job_id}",
        "description": description,
    }


def test_valid_default_json_profile_matches_accepted_behavior() -> None:
    profile = get_default_profile()
    job = Job(
        seek_job_id="unused",
        fallback_key="unused",
        title="Strategy Analyst",
        url="https://www.seek.com.au/job/unused",
        description="Strategy work.",
    )
    result = evaluate_job_content(job, profile)

    assert profile.id == "early_career_strategy_commercial_growth_ops"
    assert profile.version == "2026-07-13.2"
    assert result["score"] == 100
    assert result["outcome"] == RuleOutcome.STRONG_MATCH.value


def test_validation_rejects_malformed_missing_duplicate_and_unsupported(tmp_path: Path) -> None:
    malformed = tmp_path / "bad.json"
    with pytest.raises(ProfileValidationError, match="malformed JSON"):
        from app.rules import load_profile_file

        load_profile_file(_write_profile(tmp_path, "bad.json", "{not json"))

    missing = _default_data()
    missing.pop("id")
    with pytest.raises(ProfileValidationError, match="missing required"):
        validate_profile_data(missing, malformed)

    duplicate = _default_data()
    duplicate["rules"][1]["id"] = duplicate["rules"][0]["id"]
    with pytest.raises(ProfileValidationError, match="duplicate rule IDs"):
        validate_profile_data(duplicate, malformed)

    unsupported = _default_data()
    unsupported["rules"][0]["fields"] = ["salary"]
    with pytest.raises(ProfileValidationError, match="unsupported field"):
        validate_profile_data(unsupported, malformed)

    bad_match = _default_data()
    bad_match["rules"][0]["match_type"] = "contains"
    with pytest.raises(ProfileValidationError, match="unsupported match_type"):
        validate_profile_data(bad_match, malformed)


def test_validation_rejects_invalid_weights_thresholds_caps_and_ceilings(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    bad_weight = _default_data()
    bad_weight["rules"][0]["weight"] = 1000
    with pytest.raises(ProfileValidationError, match="weight"):
        validate_profile_data(bad_weight, path)

    bad_threshold = _default_data()
    bad_threshold["thresholds"]["review"] = 80
    with pytest.raises(ProfileValidationError, match="ordered"):
        validate_profile_data(bad_threshold, path)

    bad_cap = _default_data()
    bad_cap["max_description_positive_rules"] = -1
    with pytest.raises(ProfileValidationError, match="max_description_positive_rules"):
        validate_profile_data(bad_cap, path)

    bad_ceiling = _default_data()
    bad_ceiling["title_seniority_outcome_ceilings"]["senior"] = "exclude"
    with pytest.raises(ProfileValidationError, match="invalid outcome"):
        validate_profile_data(bad_ceiling, path)


def test_validation_rejects_unsafe_or_uncontrolled_regex(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    validate_profile_data(_default_data(), path)

    uncontrolled = _default_data()
    uncontrolled["rules"][0]["match_type"] = "regex"
    uncontrolled["rules"][0]["patterns"] = [r"\bstrategy\b"]
    with pytest.raises(ProfileValidationError, match="not allowed to use regex"):
        validate_profile_data(uncontrolled, path)

    too_many = _default_data()
    years_rule = next(rule for rule in too_many["rules"] if rule["id"] == "years_experience")
    years_rule["patterns"] = [r"\b6\s+years\b"] * 5
    with pytest.raises(ProfileValidationError, match="at most 4 regex"):
        validate_profile_data(too_many, path)

    unsafe_patterns = [
        r"\b(.+)+years\b",
        r"\b(a|aa)+\b",
        r"(?=senior)senior",
        r"\b(\w+)\s+\1\b",
        "x" * 181,
    ]
    for pattern in unsafe_patterns:
        unsafe = _default_data()
        seniority_rule = next(rule for rule in unsafe["rules"] if rule["id"] == "seniority_title")
        seniority_rule["patterns"] = [pattern]
        with pytest.raises(ProfileValidationError, match="unsupported constructs|too long"):
            validate_profile_data(unsafe, path)


def test_registry_handles_invalid_optional_duplicate_and_no_valid_profiles(tmp_path: Path) -> None:
    first = _default_data()
    second = _default_data()
    second["name"] = "Duplicate default"
    invalid = _default_data()
    invalid["rules"][0]["patterns"] = []
    _write_profile(tmp_path, "a.json", first)
    _write_profile(tmp_path, "b.json", second)
    _write_profile(tmp_path, "invalid.json", invalid)

    registry = load_profile_registry(tmp_path)

    assert len(registry.valid_profiles) == 1
    assert registry.errors
    assert registry.duplicate_errors

    empty_registry = load_profile_registry(tmp_path / "missing")
    assert not empty_registry.valid_profiles
    assert empty_registry.errors


def test_fingerprint_semantics_and_incremented_version(tmp_path: Path) -> None:
    data = _default_data()
    reordered = json.loads(json.dumps(data, sort_keys=True))
    changed = _default_data()
    changed["rules"][0]["weight"] += 1
    incremented = _default_data()
    incremented["version"] = "2026-07-13.3"

    first = validate_profile_data(data, tmp_path / "first.json")
    second = validate_profile_data(reordered, tmp_path / "second.json")
    third = validate_profile_data(changed, tmp_path / "third.json")
    fourth = validate_profile_data(incremented, tmp_path / "fourth.json")

    assert first.fingerprint == second.fingerprint
    assert first.fingerprint != third.fingerprint
    assert fourth.version.endswith(".3")


def test_fingerprint_conflict_rejects_reused_id_version_and_legacy_is_readable() -> None:
    session_factory = _session_factory()
    profile = get_default_profile()
    changed_profile = replace(profile, fingerprint="different")
    with session_factory() as db:
        run = create_search_and_run(db, "strategy", "Sydney", "last_7_days", 1)
        job = save_job_discovery(db, run.id, 1, _job_payload("991", "Strategy Analyst"))
        evaluation = evaluate_and_store_job(db, job, profile)
        legacy = JobRuleEvaluation(
            job_id=job.id,
            score=50,
            outcome="review",
            positive_evidence=[],
            penalty_evidence=[],
            exclusion_evidence=[],
            explanation="legacy",
            profile_id="legacy",
            profile_version="1",
            profile_fingerprint=None,
            content_fingerprint=evaluation.content_fingerprint,
        )
        db.add(legacy)
        db.commit()

        legacy_profile = replace(profile, id="legacy", version="1")
        assert evaluation_state(job, legacy, legacy_profile) == "evaluated"
        with pytest.raises(ProfileSelectionError, match="content changed"):
            evaluate_and_store_job(db, job, changed_profile)


def test_explicit_profile_selection_and_unrelated_profile_state() -> None:
    session_factory = _session_factory()
    default = get_default_profile()
    custom = replace(default, id="custom_profile", version="1", base_score=10)
    with session_factory() as db:
        run = create_search_and_run(db, "strategy", "Sydney", "last_7_days", 1)
        job = save_job_discovery(db, run.id, 1, _job_payload("992", "Strategy Analyst"))
        default_eval = evaluate_and_store_job(db, job, default)
        custom_eval = evaluate_and_store_job(db, job, custom)

        assert default_eval.profile_id == default.id
        assert custom_eval.profile_id == "custom_profile"
        assert custom_eval.score < default_eval.score
        assert evaluation_state(job, default_eval, default) == "evaluated"
        assert evaluation_state(job, default_eval, custom) == "stale"
        assert db.scalars(select(JobRuleEvaluation)).all()


def test_validate_rules_cli_success_and_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    from app.cli import main as cli_main

    _write_profile(tmp_path, "default.json", _default_data())
    monkeypatch.setattr(
        sys,
        "argv",
        ["app.cli", "validate-rules", "--profile-dir", str(tmp_path)],
    )
    cli_main()
    assert "VALID early_career_strategy_commercial_growth_ops" in capsys.readouterr().out

    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    bad = _default_data()
    bad["rules"][0]["patterns"] = []
    _write_profile(bad_dir, "bad.json", bad)
    monkeypatch.setattr(
        sys,
        "argv",
        ["app.cli", "validate-rules", "--profile-dir", str(bad_dir)],
    )
    with pytest.raises(SystemExit):
        cli_main()
    assert "INVALID" in capsys.readouterr().err
