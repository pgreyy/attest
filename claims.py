"""Phase 5: soft check of figures the agent states against the ledger.

transform_llm_output in this Hermes version is called once per turn with
response_text, session_id, model, platform and turn_id. It returns a replacement
string (the first non-empty one wins) or None to leave the reply alone.

This only ever appends one short note after the reply. It never rewrites or
deletes the agent's text, and it only flags a figure it can tie to exactly one
ledger field:
  - the figure carries $, USD, USDT or %, and
  - its clause (split at line breaks, sentence ends, ';', ', ', parentheses)
    holds no other figure, names exactly one of cash / equity / balance /
    realized / unrealized / P&L-profit-loss-return, and none of the words that
    make it a different number (limit, fee, price, starting, today, if, ...).
Figures that depend on marks (equity, unrealized, total P&L, return %) are only
checked while every position has a live mark. Tolerance: 1 USDT or 0.5% for
money, whichever is larger; 0.05 percentage points for return %.

No Hermes imports: scripts/ledger_check.py drives this module directly.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

LABELS = {"cash": "cash", "equity": "equity", "realized": "realized P&L", "unrealized": "unrealized P&L",
          "total": "total P&L", "return_pct": "return"}
_MARK_DEPENDENT = {"equity", "unrealized", "total", "return_pct"}
_PNL_FIELDS = {"realized", "unrealized", "total", "return_pct"}

_FIGURE = re.compile(
    r"(?<![\w.,])(?P<sign>[-−+])?\s?(?P<cur>\$|USDT\s?|USD\s?)?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s?(?P<suf>%|USDT\b|USD\b|dollars\b))?(?![\w%])",
    re.IGNORECASE)
_SPLIT = re.compile(r"\n|;|,\s|\.\s|\.$|[()\[\]]|\s[–—]\s|\s-\s")
_EXCLUDE = re.compile(
    r"\b(limits?|max|maximum|min|minimum|fees?|prices?|bid|ask|quotes?|avg|average|cost|per|target|if|would|"
    r"could|should|might|starting|start|started|initial|deposit(?:ed)?|today|today's|daily|yesterday|week|"
    r"month|orders?|notional|size|worth|budget|stop|bought|buy|sold|sell|spent|spend|paid|proceeds|"
    r"trading|trades?|at)\b|@")
_UNREALIZED = re.compile(r"\bunreali[sz]ed\b")
_REALIZED = re.compile(r"(?<!un)\breali[sz]ed\b")
_PNL = re.compile(r"\bp\s?&\s?l\b|\bp/l\b|\bpnl\b|\bprofits?\b|\blosse?s?\b|\breturns?\b")
_PNL_PHRASE = re.compile(r"\bprofit\s?(?:and|&|/)\s?loss\b|\bp\s?&\s?l\b|\bp/l\b|\bpnl\b")
_NEGATIVE = re.compile(r"\b(loss|losses|lost|losing|down|negative|drawdown)\b")
_POSITIVE = re.compile(r"\b(profit|profits|gain|gains|gained|up|positive|made|earned)\b")
_EQUITY = re.compile(r"\bequity\b")
_CASH = re.compile(r"\bcash\b")
_BALANCE = re.compile(r"\bbalance\b")


def _clauses(text: str):
    clean = re.sub(r"[*_`|>#]+", " ", text)
    for part in _SPLIT.split(clean):
        if part and part.strip():
            yield part.strip()


def _field(clause_lower: str, unit: str):
    """The one ledger field this clause's figure belongs to, or None."""
    if _EXCLUDE.search(clause_lower):
        return None
    fields = set()
    has_unreal, has_real = bool(_UNREALIZED.search(clause_lower)), bool(_REALIZED.search(clause_lower))
    if has_unreal:
        fields.add("unrealized")
    if has_real:
        fields.add("realized")
    if _PNL.search(clause_lower) and not (has_unreal or has_real):
        fields.add("return_pct" if unit == "%" else "total")
    has_equity, has_cash = bool(_EQUITY.search(clause_lower)), bool(_CASH.search(clause_lower))
    if has_equity:
        fields.add("equity")
    if has_cash:
        fields.add("cash")
    if _BALANCE.search(clause_lower) and not (has_equity or has_cash):
        fields.add("balance")
    if len(fields) != 1:
        return None
    field = fields.pop()
    if (unit == "%") != (field == "return_pct"):
        return None  # a % beside cash/equity, or a $ beside "return %": not one ledger figure
    return field


def _direction(clause_lower: str):
    """-1, +1 or None (unknown) for a P&L figure stated without a sign."""
    words = _PNL_PHRASE.sub(" ", clause_lower)
    neg, pos = bool(_NEGATIVE.search(words)), bool(_POSITIVE.search(words))
    if neg == pos:
        return None
    return -1 if neg else 1


def find_claims(text: str) -> list:
    """[(field, stated Decimal, signed: bool, raw text)] for figures tied to one ledger field."""
    out = []
    for clause in _clauses(text):
        figures = list(_FIGURE.finditer(clause))
        figures = [m for m in figures if m.group("cur") or m.group("suf")]
        if len(figures) != 1:
            continue
        m = figures[0]
        unit = "%" if m.group("suf") == "%" else "$"
        lower = clause.lower()
        field = _field(lower, unit)
        if field is None:
            continue
        try:
            value = Decimal(m.group("num").replace(",", ""))
        except InvalidOperation:
            continue
        signed = False
        if m.group("sign") in ("-", "−"):
            value, signed = -value, True
        elif m.group("sign") == "+":
            signed = True
        elif field in _PNL_FIELDS:
            d = _direction(lower)
            if d is not None:
                value, signed = value * d, True
        out.append((field, value, signed, m.group(0).strip()))
    return out


def _close(stated: Decimal, real: Decimal, field: str, signed: bool) -> bool:
    if field in _PNL_FIELDS and not signed:
        stated, real = abs(stated), abs(real)
    if field == "return_pct":
        return abs(stated - real) <= Decimal("0.05")
    tol = max(Decimal("1"), abs(real) * Decimal("0.005"))
    return abs(stated - real) <= tol


def _show(field: str, value: Decimal) -> str:
    if field == "return_pct":
        return f"{value.quantize(Decimal('0.01'))}%"
    return f"{value.quantize(Decimal('0.01'))} USDT"


def check(text: str, figures: dict):
    """The note to append, or None when every grounded figure agrees with the ledger."""
    live = bool(figures.get("marks_live"))
    problems, seen = [], set()
    for field, stated, signed, raw in find_claims(text):
        if (field, raw) in seen:
            continue
        seen.add((field, raw))
        if field == "balance":
            # "balance" may mean cash or equity: flag only if it matches neither.
            if not live:
                continue
            if _close(stated, figures["cash"], "cash", True) or _close(stated, figures["equity"], "equity", True):
                continue
            problems.append(f"cash {_show('cash', figures['cash'])} and equity "
                            f"{_show('equity', figures['equity'])}, not {raw}")
            continue
        if field in _MARK_DEPENDENT and not live:
            continue
        real = figures[field]
        if not _close(stated, real, field, signed):
            shown = raw if (stated >= 0 or raw.startswith(("-", "−"))) else "-" + raw
            problems.append(f"{LABELS[field]} {_show(field, real)}, not {shown}")
    if not problems:
        return None
    return "attest check: the ledger shows " + "; ".join(problems) + "."
