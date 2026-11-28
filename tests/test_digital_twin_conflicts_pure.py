"""Cross-source conflict reporting (Step K v2 slice 7). Pure tests, no DB."""

import pytest

from app.digital_twin import conflicts


def _view(source, entities):
    return {"source": source, "entities": entities}


def _entity(entity_id, kind, name, attributes=None):
    return {"id": entity_id, "kind": kind, "name": name, "attributes": attributes or {}}


def test_agreeing_sources_have_no_conflict():
    result = conflicts.detect_conflicts([
        _view("company_graph", [_entity("e-1", "employee", "Ayesha", {"department": "Finance"})]),
        _view("finance", [_entity("e-1", "employee", "Ayesha", {"department": "Finance"})]),
    ])
    assert result["total"] == 0
    assert result["conflicts"] == []
    assert result["entities_compared"] == 1


def test_single_source_presence_is_not_a_conflict():
    result = conflicts.detect_conflicts([
        _view("company_graph", [_entity("e-1", "employee", "Ayesha")]),
        _view("finance", []),
    ])
    assert result["total"] == 0


def test_name_and_attribute_disagreements_list_both_sides():
    result = conflicts.detect_conflicts([
        _view("company_graph", [_entity("e-1", "employee", "Ayesha", {"department": "Finance"})]),
        _view("finance", [_entity("e-1", "employee", "Ayesha Khan", {"department": "Ops"})]),
    ])
    assert result["total"] == 2
    by_field = {c["field"]: c for c in result["conflicts"]}
    assert by_field["name"]["values"] == {"company_graph": "Ayesha", "finance": "Ayesha Khan"}
    assert by_field["attributes.department"]["values"] == {"company_graph": "Finance", "finance": "Ops"}
    assert by_field["name"]["kinds"] == {"company_graph": "employee", "finance": "employee"}


def test_kind_disagreement_is_a_conflict():
    result = conflicts.detect_conflicts([
        _view("company_graph", [_entity("e-1", "employee", "Ayesha")]),
        _view("finance", [_entity("e-1", "system", "Ayesha")]),
    ])
    assert result["total"] == 1
    assert result["conflicts"][0]["field"] == "kind"
    assert result["conflicts"][0]["values"] == {"company_graph": "employee", "finance": "system"}


def test_missing_attribute_key_counts_as_disagreement():
    result = conflicts.detect_conflicts([
        _view("company_graph", [_entity("e-1", "employee", "Ayesha", {"department": "Finance"})]),
        _view("finance", [_entity("e-1", "employee", "Ayesha")]),
    ])
    assert result["total"] == 1
    assert result["conflicts"][0]["field"] == "attributes.department"


def test_money_amounts_compared_by_value_not_representation():
    # "100.00" vs 100 vs 100.0 are the same amount: no conflict.
    result = conflicts.detect_conflicts([
        _view("company_graph", [_entity("b-1", "invoice_batch", "Batch 1", {"amount": "100.00"})]),
        _view("finance", [_entity("b-1", "invoice_batch", "Batch 1", {"amount": 100})]),
    ])
    assert result["total"] == 0
    result = conflicts.detect_conflicts([
        _view("company_graph", [_entity("b-1", "invoice_batch", "Batch 1", {"amount": 100})]),
        _view("finance", [_entity("b-1", "invoice_batch", "Batch 1", {"amount": 120})]),
    ])
    assert result["total"] == 1
    assert result["conflicts"][0]["field"] == "attributes.amount"


def test_money_amount_validation_matches_drift():
    with pytest.raises(ValueError):
        conflicts.detect_conflicts([
            _view("finance", [_entity("b-1", "invoice_batch", "Batch 1", {"amount": "not-money"})]),
            _view("company_graph", [_entity("b-1", "invoice_batch", "Batch 1", {"amount": 100})]),
        ])
    with pytest.raises(ValueError):
        conflicts.detect_conflicts([
            _view("finance", [_entity("b-1", "invoice_batch", "Batch 1", {"amount": 100.001})]),
            _view("company_graph", [_entity("b-1", "invoice_batch", "Batch 1", {"amount": 100})]),
        ])


def test_cap_and_deterministic_order():
    views = [
        _view("company_graph", [_entity(f"e-{i}", "employee", f"Name {i}") for i in range(5)]),
        _view("finance", [_entity(f"e-{i}", "employee", f"Other {i}") for i in range(5)]),
    ]
    result = conflicts.detect_conflicts(views, max_conflicts=2)
    assert result["total"] == 5
    assert result["truncated"] is True
    assert len(result["conflicts"]) == 2
    assert [c["entity_id"] for c in result["conflicts"]] == ["e-0", "e-1"]


def test_long_values_bounded():
    result = conflicts.detect_conflicts([
        _view("company_graph", [_entity("e-1", "employee", "x" * 500)]),
        _view("finance", [_entity("e-1", "employee", "y" * 500)]),
    ])
    assert len(result["conflicts"][0]["values"]["company_graph"]) == 200


def test_malformed_inputs_raise():
    with pytest.raises(ValueError):
        conflicts.detect_conflicts("not-a-list")
    with pytest.raises(ValueError):
        conflicts.detect_conflicts([{"entities": []}])
    with pytest.raises(ValueError):
        conflicts.detect_conflicts([
            _view("finance", [_entity("e-1", "employee", "A")]),
            _view("finance", [_entity("e-2", "employee", "B")]),
        ])
    with pytest.raises(ValueError):
        conflicts.detect_conflicts([_view("finance", [{"kind": "employee", "name": "A"}])])
    with pytest.raises(ValueError):
        conflicts.detect_conflicts([_view("finance", [_entity("e-1", "employee", "A", {"x": {"nested": 1}})])])
    with pytest.raises(ValueError):
        conflicts.detect_conflicts([_view("finance", [])], max_conflicts=0)


def test_redaction_owner_sees_all_non_owner_sees_disagreement_only():
    result = conflicts.detect_conflicts([
        _view("company_graph", [
            _entity("b-1", "invoice_batch", "Batch 1", {"amount": 100}),
            _entity("e-1", "employee", "Ayesha"),
        ]),
        _view("finance", [
            _entity("b-1", "invoice_batch", "Batch 1", {"amount": 120}),
            _entity("e-1", "employee", "Ayesha Khan"),
        ]),
    ])
    owner = conflicts.redact_conflicts_for_viewer(result, is_owner=True)
    assert owner is result
    assert "money_redacted" not in owner
    viewer = conflicts.redact_conflicts_for_viewer(result, is_owner=False)
    by_field = {c["field"]: c for c in viewer["conflicts"]}
    assert by_field["attributes.amount"]["values"] == {"company_graph": "<redacted>", "finance": "<redacted>"}
    assert by_field["name"]["values"] == {"company_graph": "Ayesha", "finance": "Ayesha Khan"}
    assert viewer["money_redacted"] is True


def test_conflict_summary_is_count_only():
    result = conflicts.detect_conflicts([
        _view("company_graph", [_entity("e-1", "employee", "A")]),
        _view("finance", [_entity("e-1", "employee", "B")]),
    ])
    assert conflicts.conflict_summary(result) == "1 conflict(s) across 1 compared entity"
