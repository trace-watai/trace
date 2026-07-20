// Dashboard data contracts, align with the backend run artifacts.
// Each module documents the Python schema it tracks and exposes a `Raw*` wire
// type & a camelCase domain type
export * from "./attribution";
export * from "./evidence";
export * from "./severity";
export * from "./failure-card";
export * from "./run-result";
export * from "./verifier-result";
export * from "./repair-package";
export * from "./regression-artifact";
export * from "./trace-event";
