# `^0.0.3` matches 0.0.4, 0.0.9, ... (should only match 0.0.3)

We use `tinysemver` in our internal package resolver. A service depends on `"left-align": "^0.0.3"` and our resolver installed `0.0.12`, which had breaking changes. npm itself resolves the same range to `0.0.3`.

### Reproduce

```js
import { satisfies, maxSatisfying, validRange } from "tinysemver";

console.log(satisfies("0.0.9", "^0.0.3"));
console.log(maxSatisfying(["0.0.3", "0.0.4", "0.0.12", "0.1.0"], "^0.0.3"));
console.log(validRange("^0.0.3"));
```

### Actual

```
true
0.0.12
>=0.0.3 <0.1.0-0
```

### Expected

```
false
0.0.3
>=0.0.3 <0.0.4-0
```

The npm docs say a caret range allows changes that do not modify the left-most non-zero part of the version, so for `0.0.x` versions only that exact patch is allowed. The README says caret ranges follow npm's rules.

Other caret ranges look right: `^1.2.3` stops before 2.0.0, and `^0.2.3` stops before 0.3.0. Partial ranges like `^0.0` (any 0.0.x) and `^0` (any 0.x.y) also look right to us, so please don't narrow those.

tinysemver 1.3.0, Node 20.
