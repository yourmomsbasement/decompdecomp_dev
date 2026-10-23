"""Which model is more capable, and which tier a claim should come from.

Two ways to answer that, and the app keeps both:

- **auto** (the default): the win rate a model has shown against the others in this project decides, as it always
  did (`network._route`): a model well behind the best leaves the functions others gave up on alone.
- **manual**: an ordered list of models, most capable first, written by the user who knows. The stronger half
  finishes (it takes functions another agent already had a go at); the weaker half makes first passes (it takes
  untouched functions). Inside a half the order is only a tie-break; the statistics still sort the queue.

An agent can also be pinned with `agents.tier` ("first", "finish" or "off"), which wins over both: a specific
worker is sometimes known to be better or worse than its model's place in the list.

Nothing here touches the database: it is the arithmetic, so it can be tested on its own.
"""

from __future__ import annotations

MODE_AUTO, MODE_MANUAL, MODE_OFF = "auto", "manual", "off"
MODES = (MODE_AUTO, MODE_MANUAL, MODE_OFF)
TIERS = ("first", "finish", "off")  # what agents.tier may say
FINISH, FIRST = "finish", "first"


def mode(value) -> str:
    """A settings value as a mode: anything unrecognised means auto."""
    text = str(value or "").strip().lower()
    return text if text in MODES else MODE_AUTO


def order(value) -> list[str]:
    """The manual order as a list, most capable first. Accepts the list the settings page saves or "opus, sonnet"."""
    items = value.split(",") if isinstance(value, str) else list(value or [])
    out: list[str] = []
    for item in items:
        name = str(item).strip().lower()
        if name and name not in out:
            out.append(name)
    return out


def capability(model: str, models: list[str]) -> int | None:
    """0 for the most capable entry in the list, 1 for the next... None when the list doesn't mention the model.

    An entry matches the model exactly first (`codex`), then as part of its name (`sonnet` ranks
    `claude-sonnet-low` and `claude-sonnet-1` alike), so a list can name families instead of every variant."""
    key = (model or "").strip().lower()
    if not key:
        return None
    for i, entry in enumerate(models):
        if key == entry:
            return i
    for i, entry in enumerate(models):
        if entry and entry in key:
            return i
    return None


def tier(model: str, routing_mode: str, models: list[str]) -> str | None:
    """The tier a model's claims should come from, from the manual order; None when auto decides."""
    if mode(routing_mode) != MODE_MANUAL or len(models) < 2:
        return None
    place = capability(model, models)
    if place is None:
        return None  # a model nobody ranked: the statistics decide for it, as in auto
    return FINISH if place < len(models) / 2 else FIRST


def tier_of(agent_tier: str | None, model: str, routing_mode: str, models: list[str]) -> str | None:
    """What this claim should be: an agent's pinned tier first, else the manual order's, else None (auto)."""
    pinned = str(agent_tier or "").strip().lower()
    if pinned in TIERS:
        return None if pinned == "off" else pinned
    if mode(routing_mode) == MODE_OFF:
        return None
    return tier(model, routing_mode, models)


def describe(routing_mode: str, models: list[str]) -> dict:
    """What the settings mean, as the window shows them."""
    return {"mode": mode(routing_mode), "order": order(models),
            "tiers": {name: tier(name, routing_mode, models) for name in order(models)}}
