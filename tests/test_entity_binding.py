"""Tying a named subject to an identifier the document states for it.

A filing records a director's identification number as a fact of its own, and
records other facts about that director by name. Those are one person, and only
the identifier says so reliably.
"""

from backend.pipeline.entities import identifier_stated_by
from backend.pipeline.normalization import bind_strong_keys, normalize_fact
from backend.pipeline.schema import Context, Fact, make_fact_id


def make(attribute, value, subject="A Director", doc="doc_a", evidence=None):
    fact = Fact(
        fact_id=make_fact_id("c", doc, attribute, value, f"{doc}{attribute}{value}"),
        collection_id="c",
        subject=subject,
        subject_key=None,
        attribute=attribute,
        value_raw=value,
        value_num=None,
        unit=None,
        context=Context(),
        source_doc=doc,
        page=1,
        evidence_span=evidence if evidence is not None else f"{attribute} {value}",
        confidence=0.9,
    )
    return normalize_fact(fact)


def test_an_identifier_attribute_is_recognised_by_its_name():
    assert identifier_stated_by("director_identification_number", "01173669") == (
        "DIN:01173669"
    )
    assert identifier_stated_by("din", "01173669") == "DIN:01173669"
    assert identifier_stated_by("corporate_identity_number", "L63090DL2011PLC221234") == (
        "CIN:L63090DL2011PLC221234"
    )


def test_an_identifier_written_into_the_evidence_is_recognised():
    assert identifier_stated_by(
        "designation", "Nominee Director", "appointed, DIN: 01173669, with effect"
    ) == "DIN:01173669"


def test_a_number_that_merely_looks_like_one_is_not_an_identifier():
    """Eight digits alone is not a DIN; the attribute has to say so."""
    assert identifier_stated_by("headcount", "01173669") is None
    assert identifier_stated_by("pin_codes_covered", "18793") is None
    assert identifier_stated_by("applications_count", "80") is None


def test_facts_about_a_named_subject_take_the_identifier_it_states():
    facts = [
        make("director_identification_number", "01173669", subject="Suvir Suren Sujan"),
        make("designation", "Non-Executive Nominee Director", subject="Suvir Suren Sujan"),
        make("headcount", "500", subject="Someone Else"),
    ]

    bind_strong_keys(facts)

    assert facts[0].subject_key == "DIN:01173669"
    assert facts[1].subject_key == "DIN:01173669", "the name-keyed fact was upgraded"
    assert facts[2].subject_key == "name:someone else", "an unrelated subject is untouched"


def test_a_name_claimed_by_two_identifiers_is_left_alone():
    """The row-bleed case: conflicting identifiers must not pick a winner."""
    facts = [
        make("din", "01173669", subject="Suvir Suren Sujan", doc="a"),
        make("din", "02442753", subject="Suvir Suren Sujan", doc="a"),
        make("designation", "Director", subject="Suvir Suren Sujan", doc="a"),
    ]

    bind_strong_keys(facts)

    assert facts[2].subject_key == "name:suvir suren sujan"


def test_binding_works_even_though_the_identifier_fact_re_keys_itself():
    """The fact stating the DIN is itself re-keyed by it, hiding the link.

    The mapping therefore has to be built from the subject's name, not from
    whatever key that fact ended up with.
    """
    stating = make(
        "director_identification_number", "01173669",
        subject="Suvir Suren Sujan", evidence="DIN: 01173669",
    )
    about = make("designation", "Nominee Director", subject="Suvir Suren Sujan")

    assert stating.subject_key == "DIN:01173669", "re-keyed by its own evidence"
    assert about.subject_key == "name:suvir suren sujan"

    bind_strong_keys([stating, about])

    assert about.subject_key == "DIN:01173669"


# --- A self-reference stands for the organisation, never a person ----------


def test_a_key_says_what_kind_of_entity_it_identifies():
    from backend.pipeline.entities import ORGANISATION, PERSON, entity_kind

    assert entity_kind("DIN:01173669") == PERSON
    assert entity_kind("CIN:L63090DL2011PLC221234") == ORGANISATION
    assert entity_kind("name:acme") is None
    assert entity_kind(None) is None


def test_a_self_reference_never_binds_to_a_director():
    """A filing full of directors' numbers still is not a director."""
    from backend.pipeline.entities import ORGANISATION, dominant_entity

    keys = ["DIN:01173669", "DIN:01432123", "name:acme logistics", "name:acme logistics"]

    assert dominant_entity(keys, kind=ORGANISATION) == "name:acme logistics"
    assert dominant_entity(keys) == "DIN:01173669", "unrestricted, the strongest wins"


def test_a_self_reference_prefers_a_company_identifier_when_there_is_one():
    from backend.pipeline.entities import ORGANISATION, dominant_entity

    keys = ["DIN:01173669", "CIN:L63090DL2011PLC221234", "name:acme"]

    assert dominant_entity(keys, kind=ORGANISATION) == "CIN:L63090DL2011PLC221234"


def test_the_group_is_not_bound_to_a_directors_number():
    from backend.pipeline.normalization import resolve_self_references

    facts = [
        make("designation", "Director", subject="A Director", evidence="DIN: 01173669"),
        make("revenue", "100", subject="Acme Logistics Limited"),
        make("lease_liabilities", "13,820.08", subject="The Group"),
    ]

    resolve_self_references(facts)

    assert facts[2].subject_key == "name:acme logistics"
    assert facts[0].subject_key == "DIN:01173669", "the director keeps their own key"
