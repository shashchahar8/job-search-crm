# Rule Profiles

A rule profile is a strict JSON file that configures deterministic job scoring. Profiles are stored in `config/rule_profiles/` and are source-controlled so changes are reviewable.

Copy `example-copy.json`, give it a unique `id`, and increment `version` whenever you make a substantive change. Do not reuse the same `id` and `version` for changed content; existing evaluations store a profile fingerprint and the app will reject conflicting content until the version is incremented.

Profiles define a base score, outcome thresholds, description-positive caps, title-seniority ceilings, and rules. Rules inspect `title`, `description`, or both. `phrase` matching is normalized for case, punctuation, hyphens, apostrophes, ampersands, whitespace and word boundaries. The bundled default uses a few constrained `regex` rules for seniority and years-of-experience detection; ordinary customization should use `phrase`.

Positive weights add points. Negative weights subtract points. Description-positive matches are discounted by the engine and then capped by `max_description_positive_contribution` and `max_description_positive_rules`; all matched evidence is still stored even when it contributes zero additional score.

Hard exclusions keep the numeric score visible but force the final outcome to `exclude`. Use them only for conservative title phrases where the evidence label is explicit.

Title-seniority ceilings limit the final recommendation without automatically excluding the job. In the default profile, `senior` and `lead` are capped at `review`; `manager`, `principal`, `head of`, and `director` are capped at `weak_match`.

Thresholds map score to outcomes: below `weak_match` is `exclude`, then `weak_match`, `review`, and `strong_match`.

Validate profiles with:

```powershell
.\.venv\Scripts\python.exe -m app.cli validate-rules
```

The command reads profile JSON only. It does not start the web server, modify the database, or make network requests.

To customize:

1. Copy `example-copy.json` to a new filename.
2. Choose a unique `id`, for example `my_strategy_growth_profile`.
3. Set `version`, for example `2026-07-14.1`.
4. Add a preferred title by creating a positive `target_title` rule with `fields: ["title"]`.
5. Add a penalized title phrase with a negative `weight` and `hard_exclusion: false`.
6. Add a hard-excluded title phrase with `hard_exclusion: true` and a clear `evidence_label`.
7. Adjust weights or thresholds cautiously.
8. Increment `version` every time you change rule meaning.
9. Select the profile in the dashboard, Jobs page, or job detail before evaluating.
10. Re-evaluate stale jobs explicitly after changing profile content or version.

If validation fails, fix the reported JSON file and field. One invalid optional profile does not stop valid profiles from loading, but an explicitly selected invalid or missing profile will not be silently replaced.
