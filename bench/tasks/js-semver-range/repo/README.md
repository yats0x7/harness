# tinysemver

Parse semantic versions and match them against npm-style ranges, with no
dependencies.

```js
import { satisfies, maxSatisfying, compare } from "tinysemver";

satisfies("1.4.2", "^1.2.0");                 // true
satisfies("2.0.0", ">=1.0.0 <2.0.0 || ^3");    // false
maxSatisfying(["1.2.3", "1.3.0", "2.0.0"], "~1.2"); // "1.2.3"
```

Supported range syntax: primitive comparators (`<`, `<=`, `>`, `>=`, `=`),
hyphen ranges (`1.2.3 - 2.0.0`), x-ranges (`1.2.x`, `1.*`, `*`), tilde
(`~1.2.3`) and caret (`^1.2.3`) ranges, and `||` unions. Caret and tilde
follow the rules documented for npm's `semver` package.

Run the tests with `npm test`.
