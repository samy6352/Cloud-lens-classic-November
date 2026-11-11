# Advisor Score

The Overview landing view includes the five Azure Advisor pillars; Governance → Advisor
Score opens per-subscription details and the official total. Reliability maps to Azure's
`HighAvailability` category.

`GET /api/dashboard/advisor-score?scope=<subscription IDs>` uses the existing live
subscription entitlement check and delegated caller / managed identity context.
Export-backed reporting grants do **not** authorize this endpoint. Reading scores requires
`Microsoft.Advisor/advisorScore/read` (included in Reader) for the active Azure identity.
No roles, consent settings or deployment defaults are changed by this feature.

Values come directly from `Microsoft.Advisor/advisorScore?api-version=2023-01-01`,
`properties.lastRefreshedScore.score`, on a 0–100 scale. Recommendation-type GUID entries
are ignored. These are not recommendation counts or Well-Architected assessment scores.
No score is reconstructed from savings, counts, consumption units or missing denominators.
The API-provided `Advisor` total is displayed separately, not calculated from the pillars.

The compact pillar tiles use theme-aware category icons and bars. Bold figures remain the
source of truth: multiple subscriptions retain their min–max range (the lighter bar segment
shows its span), with explicit partial/unavailable coverage. Decorative bars and icons are
hidden from assistive technology; missing scores never receive a zero-value bar.

Multiple subscriptions show min–max **ranges** and available/selected coverage, never an
official cross-subscription average. Details retain each subscription's real scores and Azure
refresh dates. Missing, invalid, empty, unsupported and denied reads remain unavailable, not
zero. One failing subscription does not discard other subscriptions' scores.

Advisor is subscription-scoped. Billing dates, currency, tags and service filters do not
modify these scores. The panels explain this and display source dates and the read timestamp.
Reads bypass the ARM response cache and return `private, no-store`; no shared identity cache
is introduced. Existing dashboard view caching may retain the panel until Refresh views or a
filter change; the displayed checked timestamp always remains the original read time.

Validation:
`python -m pytest tests/test_advisor_score.py -q`
and `node tests/advisor_score_frontend.mjs`.
