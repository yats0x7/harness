// Ordering of versions, following semver 2.0.0 precedence rules.
import { parseStrict } from "./parse.js";

function compareIdentifiers(a, b) {
  const aNum = typeof a === "number";
  const bNum = typeof b === "number";
  if (aNum && bNum) return a === b ? 0 : a < b ? -1 : 1;
  if (aNum) return -1;
  if (bNum) return 1;
  return a === b ? 0 : a < b ? -1 : 1;
}

function comparePrerelease(a, b) {
  if (!a.length && !b.length) return 0;
  if (!a.length) return 1; // a release sorts after its prereleases
  if (!b.length) return -1;
  const len = Math.max(a.length, b.length);
  for (let i = 0; i < len; i++) {
    if (a[i] === undefined) return -1;
    if (b[i] === undefined) return 1;
    const c = compareIdentifiers(a[i], b[i]);
    if (c !== 0) return c;
  }
  return 0;
}

export function compare(a, b) {
  const x = parseStrict(a);
  const y = parseStrict(b);
  return (
    Math.sign(x.major - y.major) ||
    Math.sign(x.minor - y.minor) ||
    Math.sign(x.patch - y.patch) ||
    comparePrerelease(x.prerelease, y.prerelease)
  );
}

export const eq = (a, b) => compare(a, b) === 0;
export const gt = (a, b) => compare(a, b) > 0;
export const gte = (a, b) => compare(a, b) >= 0;
export const lt = (a, b) => compare(a, b) < 0;
export const lte = (a, b) => compare(a, b) <= 0;

export function sort(versions) {
  return [...versions].sort(compare);
}

export function rsort(versions) {
  return [...versions].sort((a, b) => compare(b, a));
}
