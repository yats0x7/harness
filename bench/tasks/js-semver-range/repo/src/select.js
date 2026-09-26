// Picking versions out of a list.
import { compare } from "./compare.js";
import { parse } from "./parse.js";
import { Range } from "./range.js";

function candidates(versions, range, options) {
  const r = range instanceof Range ? range : new Range(range);
  return versions.filter((v) => parse(v) && r.test(v, options));
}

export function maxSatisfying(versions, range, options) {
  const found = candidates(versions, range, options);
  return found.length ? found.reduce((a, b) => (compare(a, b) >= 0 ? a : b)) : null;
}

export function minSatisfying(versions, range, options) {
  const found = candidates(versions, range, options);
  return found.length ? found.reduce((a, b) => (compare(a, b) <= 0 ? a : b)) : null;
}

export function filterSatisfying(versions, range, options) {
  return candidates(versions, range, options);
}
