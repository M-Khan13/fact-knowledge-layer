"""Eval harness tests.

The label files here are written into a temporary directory at test time and
exist only to exercise the harness's own machinery — reading a shape, scoring a
match, counting a miss. None of them is a fact set about anything, and none is
written to tests/fixtures, so nothing here can be mistaken for real labels.
The real ones come from the file the harness is pointed at.
"""

import json

import pytest

from backend.evaluation import (
    MISS_NOT_EXTRACTED,
    MISS_VALUE_MISMATCH,
    LabelFormatError,
    evaluate,
    format_report,
    load_labels,
    result_to_dict,
    values_match,
)
from backend.pipeline.normalization import normalize_fact
from backend.pipeline.schema import Context, Fact, make_fact_id


def write_labels(tmp_path, payload, name="labels.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def make_fact(attribute, value, unit="₹ Cr", doc="report_a", subject="Acme", page=1, **ctx):
    fact = Fact(
        fact_id=make_fact_id("c", doc, attribute, str(value), f"{doc}{attribute}{value}"),
        collection_id="c",
        subject=subject,
        subject_key=None,
        attribute=attribute,
        value_raw=str(value),
        value_num=None,
        unit=unit,
        context=Context(**ctx),
        source_doc=doc,
        page=page,
        evidence_span=f"{attribute} {value}",
        confidence=0.9,
    )
    return normalize_fact(fact)


# --- Reading whatever shape the file has -----------------------------------


def test_a_plain_list_of_labels_is_read(tmp_path):
    path = write_labels(
        tmp_path,
        [{"document": "report_a", "attribute": "revenue", "value": "8,142", "unit": "₹ Cr"}],
    )

    loaded = load_labels(path)

    assert len(loaded) == 1
    assert loaded.labels[0].attribute == "revenue"
    assert loaded.labels[0].document == "report_a"
    assert "list of label objects" in loaded.detected_shape


@pytest.mark.parametrize("key", ["labels", "facts", "items", "data"])
def test_labels_wrapped_in_a_key_are_found(tmp_path, key):
    path = write_labels(tmp_path, {key: [{"attribute": "revenue", "value": "1"}]})

    loaded = load_labels(path)

    assert len(loaded) == 1
    assert key in loaded.detected_shape


def test_labels_grouped_by_document_are_flattened(tmp_path):
    path = write_labels(
        tmp_path,
        {
            "report_a": [{"attribute": "revenue", "value": "1"}],
            "report_b": [{"attribute": "revenue", "value": "2"}],
        },
    )

    loaded = load_labels(path)

    assert len(loaded) == 2
    assert {label.document for label in loaded.labels} == {"report_a", "report_b"}


def test_alternative_field_names_are_recognised(tmp_path):
    path = write_labels(
        tmp_path,
        [
            {
                "file": "report_a.pdf",
                "metric": "revenue",
                "expected_value": "8,142",
                "units": "₹ Cr",
                "fiscal_period": "FY24",
                "page_number": 12,
            }
        ],
    )

    loaded = load_labels(path)
    label = loaded.labels[0]

    assert label.document == "report_a.pdf"
    assert label.attribute == "revenue"
    assert label.value == "8,142"
    assert label.unit == "₹ Cr"
    assert label.period == "FY24"
    assert label.pages == [12]
    assert loaded.recognised_fields["value"] == "expected_value"


def test_field_names_are_matched_case_insensitively(tmp_path):
    path = write_labels(tmp_path, [{"Attribute": "revenue", "Value": "1", "Page": 3}])

    label = load_labels(path).labels[0]

    assert label.attribute == "revenue"
    assert label.pages == [3]


def test_numeric_values_survive_being_read(tmp_path):
    path = write_labels(tmp_path, [{"attribute": "growth", "value": 6.5, "unit": "%"}])

    assert load_labels(path).labels[0].value == "6.5"


def test_unrecognised_keys_are_reported_not_dropped(tmp_path):
    path = write_labels(
        tmp_path, [{"attribute": "revenue", "value": "1", "annotator": "farzan"}]
    )

    loaded = load_labels(path)

    assert "annotator" in loaded.unmapped_keys
    assert loaded.labels[0].raw["annotator"] == "farzan"


def test_describe_shows_the_mapping_it_used(tmp_path):
    path = write_labels(tmp_path, [{"metric": "revenue", "expected": "1"}])

    text = load_labels(path).describe()

    assert "metric" in text and "expected" in text
    assert "labels found    : 1" in text


# --- Refusing to guess -----------------------------------------------------


def test_a_missing_file_is_an_error_not_an_empty_run():
    """Without labels there is nothing to score, and none will be invented."""
    with pytest.raises(FileNotFoundError, match="No label file"):
        load_labels("tests/fixtures/definitely-not-here.json")


def test_invalid_json_is_reported_clearly(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(LabelFormatError, match="not valid JSON"):
        load_labels(path)


def test_an_unreadable_shape_names_the_keys_it_saw(tmp_path):
    path = write_labels(tmp_path, {"metadata": {"version": 1}})

    with pytest.raises(LabelFormatError) as exc:
        load_labels(path)
    assert "metadata" in str(exc.value)


def test_labels_without_an_attribute_or_value_are_rejected(tmp_path):
    path = write_labels(tmp_path, [{"note": "something", "who": "someone"}])

    with pytest.raises(LabelFormatError, match="no attribute or value"):
        load_labels(path)


def test_an_empty_label_list_is_rejected(tmp_path):
    with pytest.raises(LabelFormatError, match="no labels"):
        load_labels(write_labels(tmp_path, []))


# --- Scoring ---------------------------------------------------------------


def label_from(**fields):
    from backend.evaluation import Label

    return Label(
        document=fields.get("document", "report_a"),
        subject=fields.get("subject"),
        attribute=fields.get("attribute"),
        value=fields.get("value"),
        unit=fields.get("unit"),
        period=fields.get("period"),
        scope=fields.get("scope"),
        basis=fields.get("basis"),
        vintage=fields.get("vintage"),
        pages=fields.get("pages", [fields["page"]] if fields.get("page") else []),
        raw={},
    )


def test_an_exact_label_is_matched():
    labels = [label_from(attribute="revenue", value="8,142", unit="₹ Cr")]
    facts = [make_fact("revenue", "8,142")]

    result = evaluate(labels, facts)

    assert result.true_positives == 1
    assert result.recall == 1.0
    assert result.misses == []


def test_a_label_matches_across_units():
    """The harness uses the pipeline's own idea of the same figure."""
    labels = [label_from(attribute="revenue", value="8,142", unit="₹ Cr")]
    facts = [make_fact("revenue", "81,415.38", unit="₹ million")]

    assert evaluate(labels, facts).true_positives == 1


def test_a_differently_worded_attribute_still_matches():
    labels = [label_from(attribute="revenue_from_operations", value="8,142", unit="₹ Cr")]
    facts = [make_fact("operating_revenue", "8,142")]

    result = evaluate(labels, facts)

    assert result.true_positives == 1
    assert not result.matches[0].exact_attribute


def test_a_wrong_value_is_a_miss_that_says_what_was_extracted():
    """The most useful miss: the engine found it and got the number wrong."""
    labels = [label_from(attribute="revenue", value="8,142", unit="₹ Cr")]
    facts = [make_fact("revenue", "9,500")]

    result = evaluate(labels, facts)

    assert result.true_positives == 0
    assert len(result.misses) == 1
    assert result.misses[0].cause == MISS_VALUE_MISMATCH
    assert result.misses[0].nearest is facts[0]
    assert "9,500" in result.misses[0].note


def test_an_absent_label_is_a_plain_miss():
    labels = [label_from(attribute="headcount", value="1200", unit="people")]
    facts = [make_fact("revenue", "8,142")]

    result = evaluate(labels, facts)

    assert result.misses[0].cause == MISS_NOT_EXTRACTED
    assert result.misses[0].nearest is None


def test_labels_only_match_their_own_document():
    labels = [label_from(document="report_a", attribute="revenue", value="8,142", unit="₹ Cr")]
    facts = [make_fact("revenue", "8,142", doc="report_b")]

    assert evaluate(labels, facts).true_positives == 0


def test_a_label_naming_no_document_matches_anywhere():
    labels = [label_from(document=None, attribute="revenue", value="8,142", unit="₹ Cr")]
    facts = [make_fact("revenue", "8,142", doc="whichever")]

    assert evaluate(labels, facts).true_positives == 1


def test_a_period_mismatch_prevents_a_match():
    labels = [label_from(attribute="revenue", value="8,142", unit="₹ Cr", period="FY24")]
    facts = [make_fact("revenue", "8,142", period="FY25")]

    result = evaluate(labels, facts)

    assert result.true_positives == 0
    assert result.misses[0].cause == MISS_VALUE_MISMATCH


def test_equivalent_period_notations_still_match():
    labels = [label_from(attribute="revenue", value="8,142", unit="₹ Cr", period="FY24")]
    facts = [make_fact("revenue", "8,142", period="FY 2023-24")]

    assert evaluate(labels, facts).true_positives == 1


def test_one_fact_cannot_satisfy_two_labels():
    """Otherwise recall could exceed what was actually extracted."""
    labels = [
        label_from(attribute="revenue", value="8,142", unit="₹ Cr"),
        label_from(attribute="revenue", value="8,142", unit="₹ Cr"),
    ]
    facts = [make_fact("revenue", "8,142")]

    result = evaluate(labels, facts)

    assert result.true_positives == 1
    assert len(result.misses) == 1


def test_a_page_disagreement_is_flagged_without_failing_the_match():
    labels = [label_from(attribute="revenue", value="8,142", unit="₹ Cr", page=12)]
    facts = [make_fact("revenue", "8,142", page=7)]

    result = evaluate(labels, facts)

    assert result.true_positives == 1
    assert len(result.page_disagreements) == 1
    assert result.page_disagreements[0].fact.page == 7


# --- Metrics ---------------------------------------------------------------


def test_precision_and_recall_are_computed_from_the_counts():
    labels = [
        label_from(attribute="revenue", value="8,142", unit="₹ Cr"),
        label_from(attribute="ebitda", value="1,266", unit="₹ Cr"),
    ]
    facts = [
        make_fact("revenue", "8,142"),
        make_fact("ebitda", "9,999"),
        make_fact("headcount", "1200", unit="people"),
    ]

    result = evaluate(labels, facts)

    assert result.true_positives == 1
    assert result.recall == pytest.approx(0.5)
    assert result.precision == pytest.approx(1 / 3)


def test_scoped_precision_ignores_attributes_nobody_labelled():
    """A partial label set must not be read as the engine inventing facts."""
    labels = [label_from(attribute="revenue", value="8,142", unit="₹ Cr")]
    facts = [
        make_fact("revenue", "8,142"),
        make_fact("headcount", "1200", unit="people"),
    ]

    result = evaluate(labels, facts)

    assert result.precision == pytest.approx(0.5), "headcount counts against it"
    assert result.scoped_precision == 1.0, "but nobody labelled headcount"


def test_an_unmatched_fact_in_a_labelled_attribute_does_count_against_precision():
    labels = [label_from(attribute="revenue", value="8,142", unit="₹ Cr")]
    facts = [make_fact("revenue", "8,142"), make_fact("revenue", "1", doc="report_b")]

    result = evaluate(labels, facts)

    assert len(result.scoped_spurious) == 1
    assert result.scoped_precision == pytest.approx(0.5)


def test_metrics_are_zero_rather_than_undefined_when_nothing_matches():
    result = evaluate([label_from(attribute="revenue", value="1")], [])

    assert result.recall == 0.0
    assert result.precision == 0.0
    assert result.f1 == 0.0


# --- Reporting -------------------------------------------------------------


def test_the_report_names_both_kinds_of_miss():
    labels = [
        label_from(attribute="revenue", value="8,142", unit="₹ Cr"),
        label_from(attribute="headcount", value="1200", unit="people"),
    ]
    facts = [make_fact("revenue", "9,500")]

    text = format_report(evaluate(labels, facts))

    assert "disagreed on the value" in text
    assert "Not extracted at all" in text
    assert "9,500" in text
    assert "recall" in text and "precision" in text


def test_the_report_says_when_every_label_was_found():
    labels = [label_from(attribute="revenue", value="8,142", unit="₹ Cr")]

    text = format_report(evaluate(labels, [make_fact("revenue", "8,142")]))

    assert "Every label was matched" in text


def test_results_serialise_for_tracking_over_time():
    labels = [
        label_from(attribute="revenue", value="8,142", unit="₹ Cr", page=12),
        label_from(attribute="headcount", value="1200", unit="people"),
    ]
    facts = [make_fact("revenue", "8,142", page=7)]

    payload = result_to_dict(evaluate(labels, facts))

    assert payload["totals"]["labels"] == 2
    assert payload["metrics"]["recall"] == pytest.approx(0.5)
    assert len(payload["misses"]) == 1
    assert payload["misses"][0]["cause"] == MISS_NOT_EXTRACTED
    assert payload["page_disagreements"][0]["grounded_page"] == 7
    assert payload["page_disagreements"][0]["label_pages"] == [12]
    assert json.dumps(payload)


# --- Value comparison ------------------------------------------------------


def test_values_match_handles_units_numbers_and_text():
    assert values_match(
        label_from(value="8,142", unit="₹ Cr"), make_fact("revenue", "81,415.38", unit="₹ million")
    )
    assert values_match(label_from(value="6.5", unit="%"), make_fact("g", "6.5", unit="%"))
    assert not values_match(label_from(value="6.5", unit="%"), make_fact("g", "9.9", unit="%"))


def test_values_match_falls_back_to_text_when_nothing_resolves():
    assert values_match(
        label_from(value="Yes"), make_fact("listed", "yes", unit=None)
    )
    assert not values_match(
        label_from(value="Yes"), make_fact("listed", "No", unit=None)
    )


def test_a_label_may_cite_several_pages(tmp_path):
    """A fact often appears on more than one page of the same document."""
    path = write_labels(
        tmp_path,
        [
            {"attribute": "a", "value": "1", "page": [6, 9, 17]},
            {"attribute": "b", "value": "2", "page": "6, 9, 17"},
            {"attribute": "c", "value": "3", "page": 22},
            {"attribute": "d", "value": "4"},
        ],
    )

    labels = load_labels(path).labels

    assert labels[0].pages == [6, 9, 17]
    assert labels[1].pages == [6, 9, 17]
    assert labels[2].pages == [22]
    assert labels[3].pages == []
    assert labels[0].page == 6


def test_any_cited_page_counts_as_agreement():
    labels = [label_from(attribute="revenue", value="8,142", unit="₹ Cr", pages=[6, 9, 17])]

    agreed = evaluate(labels, [make_fact("revenue", "8,142", page=9)])
    disagreed = evaluate(labels, [make_fact("revenue", "8,142", page=4)])

    assert agreed.page_disagreements == []
    assert len(disagreed.page_disagreements) == 1
