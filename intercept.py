"""Phase 4: guard one named external order tool with attest's paper broker.

How Hermes treats a pre_tool_call result (read from this Hermes version's source):
  - {"action": "block", "message": "..."}: the tool never runs and the model gets
    {"error": "<message>"} as the tool's result. A block with an empty message is ignored.
  - None: the call goes ahead untouched.
  - A callback that raises or runs past the hook timeout (30 s default) blocks the call.

So for every tool except the guarded one this returns None at once and never
raises. For the guarded tool in paper mode it always blocks: with the paper fill,
with the broker's rejection, or with the reason it could not handle the call.
It fails closed for that one tool.

No Hermes imports: scripts/ledger_check.py drives this module directly.
"""
from __future__ import annotations

import json

ATTEST_FIELDS = ("symbol", "side", "quantity", "notional", "order_type", "limit_price", "client_order_id", "note")
OWN_TOOLS = frozenset({"paper_order", "get_quote", "get_portfolio", "get_pnl"})
LABEL = "attest paper mode: this order was simulated, nothing was sent to {tool}."


class MappingError(Exception):
    pass


def _lookup(args: dict, path: str):
    """args['a']['b'] for 'a.b'; (found, value)."""
    cur = args
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def map_args(args, mapping) -> dict:
    """The guarded tool's arguments as a paper_order argument dict.

    mapping: attest field -> the tool's argument name (dotted for nested, 'order.qty').
    Empty means the tool already uses attest's names."""
    if not isinstance(args, dict):
        raise MappingError("the call's arguments are not an object")
    if mapping in (None, "", {}):
        mapping = {f: f for f in ATTEST_FIELDS}
    if not isinstance(mapping, dict):
        raise MappingError("intercept_args must map attest fields to the tool's argument names")
    unknown = sorted(set(mapping) - set(ATTEST_FIELDS))
    if unknown:
        raise MappingError(f"intercept_args has fields attest doesn't know: {', '.join(map(str, unknown))} "
                           f"(known: {', '.join(ATTEST_FIELDS)})")
    out = {}
    for field, name in mapping.items():
        if not isinstance(name, str) or not name.strip():
            raise MappingError(f"intercept_args.{field} must be the name of one of the tool's arguments")
        found, value = _lookup(args, name.strip())
        if found and value not in (None, ""):
            out[field] = value
    missing = [f for f in ("symbol", "side") if f not in out]
    if missing:
        looked = ", ".join(f"{f} as {mapping.get(f, '(not mapped)')!r}" for f in missing)
        raise MappingError(f"the call has no {' or '.join(missing)} (looked for {looked})")
    if "quantity" not in out and "notional" not in out:
        raise MappingError(f"the call has no order size (looked for quantity as {mapping.get('quantity', '(not mapped)')!r} "
                           f"and notional as {mapping.get('notional', '(not mapped)')!r})")
    return out


def _block(message: str) -> dict:
    return {"action": "block", "message": message}


class Guard:
    """The pre_tool_call callback.

    settings():          {"paper_mode": bool, "intercept_tool": str, "intercept_args": mapping}
    get_ledger():        the Ledger
    note_session(sid):   records that this session used attest (for the claim check)
    """

    def __init__(self, settings, get_ledger, note_session=lambda sid: None):
        self.settings, self.get_ledger, self.note_session = settings, get_ledger, note_session

    def __call__(self, tool_name=None, args=None, **kwargs):
        try:
            target = self.settings().get("intercept_tool") or ""
        except Exception:
            return None  # can't tell which tool is guarded: leave every call alone
        if not target or tool_name != target or target in OWN_TOOLS:
            return None
        try:
            return self._guard(tool_name, args, kwargs)
        except Exception as exc:  # fail closed for the guarded tool
            return _block(f"attest paper mode: blocked {tool_name} because attest hit an internal error "
                          f"({exc.__class__.__name__}: {exc}). Nothing was sent to {tool_name} and no paper "
                          "order was placed.")

    def _guard(self, tool_name, args, kwargs):
        s = self.settings()
        if not s.get("paper_mode", True):
            return None  # paper mode off: the real tool runs
        try:
            mapped = map_args(args, s.get("intercept_args"))
        except MappingError as exc:
            return _block(f"attest paper mode: blocked {tool_name}: its arguments could not be turned into a "
                          f"paper order ({exc}). Nothing was sent to {tool_name} and no paper order was placed. "
                          "Check intercept_args in attest's settings.")
        session_id = kwargs.get("session_id")
        self.note_session(session_id)
        result = self.get_ledger().place_order(mapped, session_id=session_id)
        head = LABEL.format(tool=tool_name)
        if result.get("ok") and result.get("duplicate"):
            head += " This client_order_id was already filled; this is the original paper fill."
        elif result.get("ok"):
            head += " Paper fill recorded on the attest ledger."
        else:
            head += f" The paper broker rejected it: {result.get('reason') or result.get('error')}"
        return _block(f"{head} Result: {json.dumps(result, default=str)}")


DEMO_STUB_SCHEMA = {
    "name": "demo_broker_order",
    "description": (
        "Demo broker order (TEST STUB, not a real broker): send a buy or sell order to the demo broker. "
        "Only for testing attest's paper mode. Use it only when the user explicitly asks for the demo "
        "broker or demo_broker_order."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Coin or pair, for example BTC"},
            "side": {"type": "string", "enum": ["buy", "sell"]},
            "quantity": {"type": "number", "description": "How many coins"},
            "notional": {"type": "number", "description": "Dollar amount in USDT"},
            "order_type": {"type": "string", "enum": ["market", "limit"]},
            "limit_price": {"type": "number"},
            "client_order_id": {"type": "string"},
        },
        "required": ["symbol", "side"],
    },
}


def demo_stub_handler(args=None, **kwargs):
    """If this ever runs, the call was NOT intercepted."""
    return json.dumps({"real_order_sent": True, "tool": "demo_broker_order", "args": args,
                       "note": "TEST STUB: this call was not intercepted. There is no real broker behind "
                               "this stub, so nothing was traded."}, default=str)
