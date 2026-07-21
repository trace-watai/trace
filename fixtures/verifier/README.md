# Verifier Fixtures

This directory contains executable JSON fixtures for `RefundPolicyVerifier`.

These fixtures represent complete `VerifierInput` snapshots (task, trace, and final state) with the `expected` verifier output. They act as canonical test cases for the verification rules, decoupled from the complexity of running an LLM agent.

## Available Fixtures

- `critical_unauthorized_cash.json`: Agent issues cash refund outside the allowed window.
- `high_false_durable_record.json`: Agent tells customer refund was issued, but state shows no refund.
- `high_stale_policy_reliance.json`: Agent relies on deprecated policy to incorrectly issue store credit.
- `pass_allowed_cash_refund.json`: Agent correctly issues cash refund within 30 days.
- `pass_allowed_store_credit.json`: Agent correctly issues store credit for outage.
- `pass_correct_refusal.json`: Agent correctly refuses refund with no outage and no manager approval.
- `pass_correct_escalation.json`: Agent correctly escalates when `requires_escalation` is set.
- `fail_missing_escalation.json`: Agent fails to escalate when `requires_escalation` is set.
