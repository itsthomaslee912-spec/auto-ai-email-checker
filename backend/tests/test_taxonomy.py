import pytest

from app.classify.openai_classifier import _heuristic_label, infer_interview_subtype
from app.models import EmailLabel


@pytest.mark.parametrize(
    ("subject", "body", "expected"),
    [
        ("Your application has been received", "Thanks for applying.", EmailLabel.APPLICATION_CONFIRMATION),
        ("Action required: complete your profile", "Upload your resume to continue your application.", EmailLabel.APPLICATION_ACTION_REQUIRED),
        ("Screening questions", "Please answer the recruiter questionnaire before we schedule an interview.", EmailLabel.SCREENING),
        ("Coding challenge", "Complete your online coding test.", EmailLabel.ASSESSMENT),
        ("Interview invitation", "Select your interview slot using the scheduling link.", EmailLabel.INTERVIEW_INVITATION),
        ("Your interview has been scheduled", "Your interview is scheduled for Friday at 2 PM.", EmailLabel.INTERVIEW_SCHEDULED),
        ("Interview follow-up", "Thank you for interviewing with us. Next steps soon.", EmailLabel.INTERVIEW_FOLLOW_UP),
        ("Offer letter", "We are pleased to offer you the role.", EmailLabel.OFFER),
        ("Position filled", "We are moving forward with other candidates.", EmailLabel.REJECTED_CLOSED),
        ("Job recommendations", "Jobs that might interest you.", EmailLabel.RECRUITMENT_ALERT),
    ],
)
def test_classification_examples(subject, body, expected):
    assert _heuristic_label(subject, "", body, "") == expected.value


@pytest.mark.parametrize(
    ("subject", "body", "subtype"),
    [
        ("Your interview has been scheduled", "Friday at 2 PM", "confirmation"),
        ("Google Calendar invitation: Interview", "Friday at 2 PM", "calendar_invite"),
        ("Reminder: Interview tomorrow at 2 PM", "", "reminder"),
        ("Your interview has been moved to Friday", "", "reschedule"),
        ("The interviewer requested a different time", "Your interview time has changed.", "time_change"),
        ("Your interview has been cancelled", "", "cancellation"),
    ],
)
def test_scheduled_interview_subtypes(subject, body, subtype):
    assert _heuristic_label(subject, "", body, "") == EmailLabel.INTERVIEW_SCHEDULED.value
    assert infer_interview_subtype(subject, "", body, "") == subtype
