"""USD cost closed loop: user-authoritative pricing, no guessed paid prices."""

from __future__ import annotations

from hub_test_support import make_project

from agentloop_hub.config import DEFAULT_PRICING, HubConfig


def test_price_for_free_builtin_paid_unknown_and_override():
    cfg = HubConfig()
    # Free models are unambiguous and built in.
    free = cfg.price_for("glm-5.3-flash-free")
    assert free["known"] is True and free["input"] == 0.0
    assert "glm-5.3-flash-free" in DEFAULT_PRICING
    # Paid models are NOT guessed: unknown by default (not free).
    assert cfg.price_for("gpt-4o")["known"] is False
    assert cfg.price_for("deepseek-v4-pro")["known"] is False
    assert cfg.price_for("deepseek-chat")["known"] is False  # retired alias, not built in
    # User config (official rate card / account billing) makes a price known.
    cfg.pricing = {"gpt-4o": {"input": 2.5, "output": 10.0}}
    assert cfg.price_for("gpt-4o")["known"] is True
    assert cfg.price_for("openai/gpt-4o")["input"] == 2.5  # provider prefix resolves
    # User override wins over a built-in free entry.
    cfg.pricing = {"glm-4-flash": {"input": 1.0, "output": 2.0}}
    assert cfg.price_for("glm-4-flash")["input"] == 1.0


def test_usd_saved_uses_user_price(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    hub.config.pricing = {"gpt-4o": {"input": 2.5, "output": 10.0}}
    cost = hub.cost
    assert cost.usd_saved(1_000_000, "gpt-4o") == 2.5  # input-side estimate
    assert cost.usd_saved(1_000_000, "deepseek-v4-pro") == 0.0  # unknown, not guessed
    assert cost.usd_saved(1_000_000, "glm-5.3-flash-free") == 0.0  # free
    assert cost.usd_saved(0, "gpt-4o") == 0.0
    hub.close()


def test_compression_records_dollar_saving_with_user_price(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    hub.config.pricing = {"gpt-4o": {"input": 2.5, "output": 10.0}}
    hub.cost.compress_context({"findings": "x" * 200000}, 200,
                              kind="context_compression", model="gpt-4o")
    totals = hub.cost.savings()["totals"]
    assert totals.get("tokens_saved", 0) > 0
    assert totals.get("cost_saved", 0) > 0
    hub.close()


def test_compression_paid_model_unknown_ledgers_tokens_only(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    # No pricing configured for a paid model: tokens ledgered, dollars honest 0.
    hub.cost.compress_context({"findings": "y" * 200000}, 200,
                              kind="context_compression", model="deepseek-v4-pro")
    totals = hub.cost.savings()["totals"]
    assert totals.get("tokens_saved", 0) > 0
    assert totals.get("cost_saved", 0) == 0
    hub.close()