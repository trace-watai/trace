/**
 * Case conversion between the backend wire format (snake_case) and the
 * frontend domain types (camelCase).
 */

type CamelCase<S extends string> = S extends `${infer Head}_${infer Tail}`
  ? `${Head}${Capitalize<CamelCase<Tail>>}`
  : S;

/** Recursively rewrite an object type's keys from snake_case to camelCase. */
export type Camelize<T> = T extends readonly (infer U)[]
  ? Camelize<U>[]
  : T extends object
    ? { [K in keyof T as CamelCase<K & string>]: Camelize<T[K]> }
    : T;

const toCamel = (key: string): string =>
  key.replace(/_([a-z0-9])/g, (_, char: string) => char.toUpperCase());

/** Deep-convert all object keys of `input` from snake_case to camelCase. */
export const camelizeKeys = <T>(input: T): Camelize<T> => {
  if (Array.isArray(input)) {
    return input.map(camelizeKeys) as Camelize<T>;
  }
  if (input !== null && typeof input === "object") {
    const entries = Object.entries(input as Record<string, unknown>).map(
      ([key, value]) => [toCamel(key), camelizeKeys(value)] as const,
    );
    return Object.fromEntries(entries) as Camelize<T>;
  }
  return input as Camelize<T>;
};
