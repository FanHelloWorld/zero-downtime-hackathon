"""The dossier: what the agent remembers about a chat between bursts.

The merge is the load-bearing part. A plan is a fresh read every time, so if the
model's summary of one burst were allowed to replace the picture, an address
given on Tuesday would vanish the moment someone changed the subject.
"""

from __future__ import annotations

from supervisor_agent.dossier import Dossier, Person, coarsen_location, merge


# ---- coarsening -----------------------------------------------------------


def test_street_address_reduces_to_the_area():
    assert coarsen_location("1234 Oak St, Fremont, CA 94536") == "Fremont, CA"


def test_unit_numbers_and_zips_go_too():
    assert coarsen_location("Apt 4B, 22 Main Street, Brooklyn NY 11201") == "Brooklyn NY"


def test_a_run_on_address_without_commas_still_loses_the_house_number():
    assert coarsen_location("1234 Oak St Fremont CA") == "Fremont CA"


def test_a_plain_place_name_is_left_alone():
    assert coarsen_location("Fremont") == "Fremont"
    assert coarsen_location("the Mission") == "the Mission"


def test_nothing_in_nothing_out():
    assert coarsen_location(None) == ""
    assert coarsen_location("   ") == ""


# ---- merging --------------------------------------------------------------


def test_a_burst_that_mentions_nobody_erases_nobody():
    before = merge(None, [{"who": "+1aaa", "location": "Fremont"}])
    after = merge(before, [])
    assert after.people["+1aaa"].location == "Fremont"


def test_later_facts_win():
    d = merge(None, [{"who": "+1aaa", "location": "Fremont"}])
    d = merge(d, [{"who": "+1aaa", "location": "Oakland"}])
    assert d.people["+1aaa"].location == "Oakland"


def test_an_unmentioned_field_survives_an_update_to_another_one():
    d = merge(None, [{"who": "+1aaa", "location": "Fremont", "availability": "after 7"}])
    d = merge(d, [{"who": "+1aaa", "note": "no shellfish"}])
    person = d.people["+1aaa"]
    assert person.location == "Fremont"
    assert person.availability == "after 7"
    assert person.notes == ["no shellfish"]


def test_new_people_are_added_not_swapped_in():
    d = merge(None, [{"who": "+1aaa", "location": "Fremont"}])
    d = merge(d, [{"who": "+1bbb", "location": "Oakland"}])
    assert set(d.people) == {"+1aaa", "+1bbb"}


def test_the_caller_s_dossier_is_not_mutated():
    original = merge(None, [{"who": "+1aaa", "location": "Fremont"}])
    merge(original, [{"who": "+1aaa", "location": "Oakland"}])
    assert original.people["+1aaa"].location == "Fremont"


def test_duplicate_notes_do_not_pile_up():
    d = merge(None, [{"who": "+1aaa", "note": "vegetarian"}])
    d = merge(d, [{"who": "+1aaa", "note": "vegetarian"}])
    assert d.people["+1aaa"].notes == ["vegetarian"]


def test_facts_without_a_speaker_are_discarded():
    d = merge(None, [{"location": "Fremont"}, {"who": "", "location": "Oakland"}, "junk"])
    assert d.people == {}


def test_the_roster_is_capped():
    facts = [{"who": f"+1{i:04d}", "location": "somewhere"} for i in range(30)]
    d = merge(None, facts)
    assert len(d.people) == 20
    assert "+10029" in d.people, "the most recent are the ones kept"


def test_vibe_updates_only_when_offered():
    d = merge(None, [], vibe="loose and hungry")
    assert d.vibe == "loose and hungry"
    assert merge(d, [], vibe="").vibe == "loose and hungry"


# ---- serialisation --------------------------------------------------------


def test_round_trips_through_json():
    d = merge(None, [{"who": "+1aaa", "name": "Ana", "location": "Fremont", "note": "no dairy"}],
              vibe="chatty")
    back = Dossier.from_json(d.to_json())
    assert back.people["+1aaa"] == Person(
        handle="+1aaa", name="Ana", location="Fremont", availability="", notes=["no dairy"]
    )
    assert back.vibe == "chatty"


def test_a_corrupt_dossier_costs_context_not_the_reply():
    assert Dossier.from_json("not json").people == {}
    assert Dossier.from_json('["wrong shape"]').people == {}
    assert Dossier.from_json(None).people == {}


# ---- the prompt view ------------------------------------------------------


def test_the_brief_coarsens_by_default():
    d = merge(None, [{"who": "+1aaa", "location": "1234 Oak St, Fremont, CA"}])
    assert "1234" not in d.brief()
    assert "Fremont" in d.brief()


def test_the_brief_can_be_asked_for_exact_locations():
    d = merge(None, [{"who": "+1aaa", "location": "1234 Oak St, Fremont, CA"}])
    assert "1234" in d.brief(coarse=False)


def test_an_empty_dossier_renders_as_nothing_at_all():
    assert Dossier().brief() == "", "an empty section in a prompt is noise"
