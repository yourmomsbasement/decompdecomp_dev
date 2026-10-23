"""The routing arithmetic: a manual order of models decides who makes first passes and who finishes."""

from decompnet import ranking


def test_order_accepts_a_list_or_a_string():
    assert ranking.order(["Opus", "sonnet", "sonnet", ""]) == ["opus", "sonnet"]
    assert ranking.order("opus, sonnet ,codex") == ["opus", "sonnet", "codex"]
    assert ranking.order(None) == []


def test_mode_falls_back_to_auto():
    assert ranking.mode("MANUAL") == "manual"
    assert ranking.mode("nonsense") == "auto"
    assert ranking.mode(None) == "auto"


def test_capability_ranks_the_list():
    models = ["opus", "sonnet", "codex"]
    assert ranking.capability("opus", models) == 0
    assert ranking.capability("codex", models) == 2
    assert ranking.capability("deepseek", models) is None


def test_an_entry_can_name_a_family():
    models = ["opus", "sonnet", "codex", "deepseek"]
    assert ranking.capability("claude-sonnet-low", models) == 1
    assert ranking.capability("api-deepseek-flash-medium", models) == 3
    assert ranking.capability("codex", models) == 2


def test_manual_order_splits_strong_and_weak():
    models = ["opus", "sonnet", "codex", "deepseek"]
    assert ranking.tier("opus", "manual", models) == "finish"
    assert ranking.tier("sonnet", "manual", models) == "finish"
    assert ranking.tier("codex", "manual", models) == "first"
    assert ranking.tier("deepseek", "manual", models) == "first"


def test_a_model_nobody_ranked_is_left_to_the_statistics():
    assert ranking.tier("gemini", "manual", ["opus", "sonnet", "codex", "deepseek"]) is None


def test_auto_and_off_never_split():
    models = ["opus", "sonnet", "codex", "deepseek"]
    assert ranking.tier("opus", "auto", models) is None
    assert ranking.tier_of(None, "opus", "off", models) is None


def test_a_pinned_agent_beats_the_model_order():
    models = ["opus", "sonnet", "codex", "deepseek"]
    assert ranking.tier_of("first", "opus", "manual", models) == "first"
    assert ranking.tier_of("finish", "deepseek", "manual", models) == "finish"
    assert ranking.tier_of("off", "opus", "manual", models) is None
    assert ranking.tier_of("", "codex", "manual", models) == "first"


def test_describe_names_every_tier():
    described = ranking.describe("manual", ["opus", "sonnet"])
    assert described["mode"] == "manual"
    assert described["tiers"] == {"opus": "finish", "sonnet": "first"}
    assert ranking.describe("auto", []) == {"mode": "auto", "order": [], "tiers": {}}
