"""The questions keep the wire shape the server expects."""

from lachesis.questions import RISK_CLASSES, grounding_question, lachesis_questions, risk_question


def test_five_risk_classes():
    assert sorted(RISK_CLASSES) == [
        "benign",
        "confused_deputy",
        "destructive_write",
        "exfiltration",
        "untrusted_to_action",
    ]
    assert all(text.strip() for text in RISK_CLASSES.values())


def test_risk_question_shape():
    question = risk_question()
    assert question["type"] == "choice"
    assert "risk class" in question["instructions"]
    assert question["criteria"] == RISK_CLASSES
    assert question["criteria"] is not RISK_CLASSES


def test_grounding_question_shape():
    question = grounding_question()
    assert question["type"] == "noul"
    assert "trace back" in question["instructions"]
    assert sorted(question["criteria"]) == ["false", "true"]


def test_both_questions_under_stable_keys():
    assert sorted(lachesis_questions()) == ["grounded", "risk_class"]
