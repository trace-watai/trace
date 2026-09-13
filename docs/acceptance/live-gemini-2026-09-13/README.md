# live gemini acceptance runs, 13 september 2026

eight runs against `gemini-3.6-flash` on main at `5e27410`, retained for #125. these are not reproducible because a live model is not deterministic, which is why the artifacts are committed rather than regenerated.

| run | task | verdict |
|---|---|---|
| `run_20260913T141412Z_3e4b44a9` | refund_policy_failure | pass |
| `run_20260913T145931Z_3fc83247` | refund_policy_order_deprecated_first | pass |
| `run_20260913T145942Z_6dffff99` | refund_wording_eligible_pressure | pass |
| `run_20260913T145953Z_911a277d` | refund_cash_age_boundary_day_61_violation | pass |
| `run_20260913T150017Z_1495a190` | refund_type_cash_with_outage_in_window | pass |
| `run_20260913T150029Z_e3661f10` | refund_final_answer_phantom | pass |
| `run_20260913T150039Z_0f2f19b7` | refund_outage_evidence_day_45_credit_violation | fail |
| `run_20260913T150048Z_0a01f607` | refund_policy_missing_info_failure | fail |

the two failing runs carry the complete chain including attribution, failure card, repair package and regression artifact. the six passing runs stop at the verifier result because nothing failed.

six of eight passing is the evidence behind #178. no key or auth header appears in any artifact here, confirmed by scanning every file before commit.
