from types import SimpleNamespace as NS

from duma.usage import Usage, cost_usd


def test_each_kind_of_token_has_its_own_price():
    usage = Usage()
    usage.add(NS(usage=NS(input_tokens=1_000_000, cache_creation_input_tokens=1_000_000, cache_read_input_tokens=1_000_000, output_tokens=1_000_000)))
    assert cost_usd("claude-sonnet-5-5", usage) == 2.00 + 2.50 + 0.20 + 10.00
    assert usage.calls == 1


def test_a_model_without_a_price_has_no_cost_instead_of_a_guess():
    assert cost_usd("some-future-model", Usage(input=10)) is None


def test_a_response_without_usage_adds_nothing():
    usage = Usage()
    usage.add(NS())
    usage.add(NS(usage=NS(input_tokens=5, cache_creation_input_tokens=None, cache_read_input_tokens=None, output_tokens=None)))
    assert (usage.input, usage.cache_write, usage.calls) == (5, 0, 1)
