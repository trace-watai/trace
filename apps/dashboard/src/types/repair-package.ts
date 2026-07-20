/**
 * Repair-package data contract.
 *
 * Mirrors `RepairPackage` in `src/trace_harness/failure_bundles/schemas.py`
 * (REPAIR_PACKAGE_SCHEMA_VERSION 0.3.0), serialized as `repair_package.json`.
 *
 */

import { camelizeKeys, type Camelize } from "@/lib/casing";

export const REPAIR_PACKAGE_SCHEMA_VERSION = "0.3.0";

/**
 * Ordered urgency levels for a repair control, most urgent first; index
 * doubles as the rank for sorting. Mirrors the backend `ControlPriority`
 * StrEnum: P0 (do before next release) → P3 (optional improvements).
 */
export const CONTROL_PRIORITIES = ["P0", "P1", "P2", "P3"] as const;

export type ControlPriority = (typeof CONTROL_PRIORITIES)[number];

/** Numeric rank for a control priority, P0 (0) → P3 (3). */
export const controlPriorityRank = (priority: ControlPriority): number =>
  CONTROL_PRIORITIES.indexOf(priority);

/**
 * Wire shape of one concrete control that would prevent this failure class,
 * defined in the backend Pydantic model (snake_case keys). Some free-text
 * fields (e.g. `installation_point`) may still become enums as the dashboard
 * settles on a closed vocabulary for them.
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
  /** Urgency, P0 (do before next release) → P3 (optional improvements). */
  priority: ControlPriority;
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
