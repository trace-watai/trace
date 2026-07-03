/**
 * Repair-package data contract.
 *
 * Mirrors `RepairPackage` in `src/trace_harness/failure_bundles/schemas.py`
 * (REPAIR_PACKAGE_SCHEMA_VERSION 0.2.0), serialized as `repair_package.json`.
 *
 */

import { camelizeKeys, type Camelize } from "@/lib/casing";

export const REPAIR_PACKAGE_SCHEMA_VERSION = "0.2.0";

/**
 * Wire shape of one concrete control that would prevent this failure class,
 * defined in the backend Pydantic model (snake_case keys). In the future
 * we will need to transform many of these raw strings to enums for better
 * UI/UX in the dashboard.
 */
export interface RawRepairControl {
  name: string;
  /** Where in the codebase/config the control installs. Possibly enum? */
  installation_point: string;
  /** The exact check the control performs, stated deterministically. */
  check: string;
  behavior_on_failure: string;
  expected_impact: string;
  why_it_prevents_recurrence: string;
  risk_or_tradeoff: string;
  /** P0 (do before next release) .. P3 (opportunistic). Should be an enum */
  priority: string;
  linked_verifier_checks: string[];
}

/** One concrete control that would prevent this failure class, camelCase. */
export type RepairControl = Camelize<RawRepairControl>;

/**
 * Wire shape of `repair_package.json`, defined in the backend Pydantic model
 * (snake_case keys).
 */
export interface RawRepairPackage {
  schema_version: string;
  run_id: string;
  task_id: string;
  summary: string;
  controls: RawRepairControl[];
  metadata: Record<string, unknown>;
}

/** "What to fix so this failure cannot recur", the camelCase domain type. */
export type RepairPackage = Camelize<RawRepairPackage>;

export const parseRepairPackage = (raw: RawRepairPackage): RepairPackage =>
  camelizeKeys(raw);
