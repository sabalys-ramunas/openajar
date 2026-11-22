"""An open the cloud has no room for (2026-11-21, openajar 0.5.3): size large failed on cx43 and cx53, the
error said to try xlarge (cx53 again), a pinned cpx41 came back as Hetzner's raw "unsupported location",
cpx42 then worked, and `bigger` offered cx53 at less an hour than the cpx42 it ran on. Each of the four,
on the fake cloud with that day's types."""

from __future__ import annotations

import pytest
from test_environments import node, opened  # noqa: F401  (node is a fixture)
from test_vms import hetzner_2026

from openajar.policy.wrap import ToolCallError

LARGE_TYPES = {"cx43", "cx53", "cpx42", "ccx33", "cpx62"}  # every type with 8 CPU and 16 GB or more


async def test_a_capacity_error_never_offers_a_size_whose_types_just_had_no_room(node):  # noqa: F811
    prov = node["prov"]
    hetzner_2026(prov)
    prov.listed = None  # the provider does not say what is orderable now
    del prov.prices["cpx41"]  # (without the list it would count as sold in nbg1)
    prov.unavailable = set(LARGE_TYPES)
    with pytest.raises(ToolCallError, match="environment_failed: capacity") as err:
        await opened(node, size="large")
    words = str(err.value)
    assert "has no room for cx43, cx53, cpx42, ccx33 or cpx62 in nbg1, fsn1 or hel1 now" in words, words
    assert "xlarge" not in words  # cx53 and cpx62, which both just had no room
    assert ('Not tried yet, so it may have room: medium (4 CPU, 8 GB, less than you asked for): '
            'environment_open(size="medium")') in words, words
    assert "Available now" not in words and "—" not in words and len(words) < 600
    assert prov.made_in == []


async def test_a_size_is_made_from_another_family_and_the_result_says_what_it_costs(node):  # noqa: F811
    prov = node["prov"]
    hetzner_2026(prov)
    prov.unavailable = {"cx43", "cx53"}
    out = await opened(node, size="large")
    m = out["machine"]
    assert out["state"] == "ready" and (m["server_type"], m["price_hourly_eur"]) == ("cpx42", 0.135), out
    assert m["fell_back"] == ("No room for cx43 or cx53 now, so this is cpx42, with at least as much, at EUR 0.1350 "
                              "an hour (4.4 times the EUR 0.0304 planned for cx43)")
    # bigger: xlarge's cheapest, cx53, just had no room, so it offers the next type, never cx53
    assert [(b["size"], b["server_type"], b["price_hourly_eur"]) for b in m["bigger"]] == [("xlarge", "cpx62", 0.25)]
    assert not m["bigger"][0].get("cheaper")


async def test_a_bigger_size_that_costs_less_an_hour_says_so(node):  # noqa: F811
    prov = node["prov"]
    hetzner_2026(prov)
    out = await opened(node, size="cpx42")
    m = out["machine"]
    assert (m["server_type"], m["size"], m.get("fell_back")) == ("cpx42", "large", None)
    big = m["bigger"][0]
    assert (big["size"], big["server_type"], big["price_hourly_eur"], big["cheaper"]) == ("xlarge", "cx53", 0.057, True)
    assert big["note"] == ("costs less an hour than this machine (EUR 0.1350), which is a dearer type; cx53 may have "
                           "no room either")


async def test_a_type_by_name_the_location_does_not_sell_says_what_is_sold_and_rents_nothing(node):  # noqa: F811
    prov = node["prov"]
    hetzner_2026(prov)
    with pytest.raises(ToolCallError) as err:
        await opened(node, size="cpx41")
    words = str(err.value)
    assert "does not sell cpx41 in nbg1 now (it sells it in ash)" in words and "cx43 (8 CPU, 16 GB" in words, words
    assert 'ask for one of those, or size="large" (8 CPU, 16 GB), which picks a type sold there. Nothing was rented' \
        in words
    assert "invalid_input" not in words and "unsupported location" not in words and prov.made_in == []
