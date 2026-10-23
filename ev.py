#!/usr/bin/env python3
"""
Delta Dash — expected-value engine for information hints.

The game reveals *structure* of random moves ("value moves up by the sum of 3
dice"), never the realized outcome. Fair value at any time =
    base + Σ EV(revealed hints).
We also track Σ Var(hints) = the uncertainty still riding on an open position
until settlement, which the trading policy uses to size risk.

A Hint has a mean and variance. Independent hints add in both. Parse common
descriptions from slides into Hints, or build them in code.

Self-test:  python3 ev.py
"""
from __future__ import annotations
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Hint:
    mean: float
    var: float
    label: str = ""

    def scaled(self, k: float) -> "Hint":
        return Hint(self.mean * k, self.var * k * k, f"{k}*({self.label})")

    def negated(self) -> "Hint":
        return self.scaled(-1)


# ---- primitive distributions (one trial) ----

def dice(n: int, sides: int = 6) -> Hint:
    """Sum of n fair s-sided dice."""
    mean = n * (sides + 1) / 2
    var = n * (sides * sides - 1) / 12
    return Hint(mean, var, f"{n}d{sides}")


def const(c: float) -> Hint:
    return Hint(float(c), 0.0, f"const({c})")


def coin(value: float, p: float = 0.5) -> Hint:
    """Pays `value` with prob p, else 0."""
    mean = p * value
    var = p * (1 - p) * value * value
    return Hint(mean, var, f"coin({value}@{p})")


def bernoulli_pm(value: float, p: float = 0.5) -> Hint:
    """+value with prob p, -value with prob 1-p (symmetric swing)."""
    mean = value * (2 * p - 1)
    var = value * value * (1 - (2 * p - 1) ** 2)
    return Hint(mean, var, f"pm({value}@{p})")


def uniform_int(a: int, b: int) -> Hint:
    n = b - a + 1
    mean = (a + b) / 2
    var = (n * n - 1) / 12
    return Hint(mean, var, f"unifint[{a},{b}]")


def uniform(a: float, b: float) -> Hint:
    return Hint((a + b) / 2, (b - a) ** 2 / 12, f"unif[{a},{b}]")


def normal(mu: float, sigma: float) -> Hint:
    return Hint(mu, sigma * sigma, f"N({mu},{sigma})")


def choice(values, probs=None) -> Hint:
    """Discrete outcome from `values` (equal prob unless `probs` given)."""
    n = len(values)
    probs = probs or [1 / n] * n
    mean = sum(v * p for v, p in zip(values, probs))
    var = sum(p * (v - mean) ** 2 for v, p in zip(values, probs))
    return Hint(mean, var, f"choice({values})")


# ---- combine ----

def combine(hints) -> Hint:
    """Independent sum."""
    m = sum(h.mean for h in hints)
    v = sum(h.var for h in hints)
    return Hint(m, v, " + ".join(h.label for h in hints))


# ---- parser for quick slide entry ----
# Supports terms joined by +/-:  "3d6", "d6", "2d10", "7", "coin:20",
# "coin:20:0.3", "pm:5", "unifint:1:6", "unif:0:10", "n:0:3", "choice:1,2,5"
_DICE = re.compile(r"^(\d*)d(\d+)$", re.I)


def parse_term(tok: str) -> Hint:
    tok = tok.strip()
    m = _DICE.match(tok)
    if m:
        n = int(m.group(1) or "1")
        return dice(n, int(m.group(2)))
    if ":" in tok:
        head, *args = tok.split(":")
        a = [float(x) if x.replace(".", "").replace("-", "").isdigit() else x for x in args]
        head = head.lower()
        if head == "coin":   return coin(a[0], a[1] if len(a) > 1 else 0.5)
        if head == "pm":     return bernoulli_pm(a[0], a[1] if len(a) > 1 else 0.5)
        if head == "unifint":return uniform_int(int(a[0]), int(a[1]))
        if head == "unif":   return uniform(a[0], a[1])
        if head == "n":      return normal(a[0], a[1])
        if head == "choice": return choice([float(x) for x in str(args[0]).split(",")])
    # bare number
    return const(float(tok))


def parse(expr: str) -> Hint:
    """Parse a '+'/'-' separated hint expression into one combined Hint.
    e.g. 'up by 3d6' -> pass '3d6'; '2d6 - 4' -> mean 3; '-3d6' -> down move."""
    expr = expr.strip().lower()
    for noise in ("up by", "down by", "moves", "move", "value", "the", "sum of", "of"):
        expr = expr.replace(noise, " ")
    expr = expr.strip()
    sign = 1
    if expr.startswith("down") or expr.startswith("-"):
        sign = -1
    expr = expr.lstrip("+-").strip()
    # split on +/- keeping signs
    terms = re.findall(r"[+-]?[^+-]+", expr.replace(" ", ""))
    hints = []
    for t in terms:
        s = -1 if t.startswith("-") else 1
        t = t.lstrip("+-")
        if not t:
            continue
        h = parse_term(t)
        hints.append(h.scaled(s))
    combined = combine(hints) if hints else const(0)
    return combined.scaled(sign) if sign == -1 else combined


if __name__ == "__main__":
    tests = ["3d6", "d6", "2d10", "coin:20", "coin:20:0.3", "pm:5",
             "unifint:1:6", "unif:0:10", "choice:1,2,5", "2d6 - 4", "up by 3d6"]
    for t in tests:
        h = parse(t)
        print(f"{t:16} -> mean {h.mean:7.3f}  var {h.var:7.3f}  std {h.var**0.5:6.3f}")
    # sanity: 3d6 EV must be 10.5
    assert abs(parse('3d6').mean - 10.5) < 1e-9
    assert abs(dice(3, 6).var - 8.75) < 1e-9
    print("ok")
