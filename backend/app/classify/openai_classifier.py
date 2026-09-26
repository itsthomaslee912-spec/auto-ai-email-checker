from __future__ import annotations

import json
import logging
import re

from openai import AsyncOpenAI

from app.classify.prompt_store import get_active_system_prompt
from app.classify.prompt import with_interview_subtype_rules
from app.models import EmailLabel
from app.schemas import ClassificationResult
from app.config import get_settings
from app.services.ai_backend import AiBackend, get_ai_backend, has_saved_ai_settings

logger = logging.getLogger(__name__)

VALID_LABELS = {label.value for label in EmailLabel}
VALID_INTERVIEW_SUBTYPES = {"confirmation", "calendar_invite", "reminder", "reschedule", "time_change", "cancellation"}

LEGACY_LABEL_MAP = {
    "tech": EmailLabel.ASSESSMENT.value,
    "available": EmailLabel.INTERVIEW_INVITATION.value,
    "alert": EmailLabel.RECRUITMENT_ALERT.value,
    "job_alert": EmailLabel.RECRUITMENT_ALERT.value,
    "applied": EmailLabel.APPLICATION_CONFIRMATION.value,
    "interview": EmailLabel.INTERVIEW_INVITATION.value,
    "rejected": EmailLabel.REJECTED_CLOSED.value,
    "others": EmailLabel.OTHER.value,
    "application_submitted": EmailLabel.APPLICATION_CONFIRMATION.value,
    "new_opportunity": EmailLabel.RECRUITMENT_ALERT.value,
    "recruiter_outreach": EmailLabel.RECRUITMENT_ALERT.value,
    "talent_pool": EmailLabel.OTHER.value,
    "company_news": EmailLabel.OTHER.value,
    "career_event": EmailLabel.OTHER.value,
    "profile_update_request": EmailLabel.APPLICATION_ACTION_REQUIRED.value,
    "withdrawn": EmailLabel.REJECTED_CLOSED.value,
    "hired": EmailLabel.OFFER.value,
    "unknown": EmailLabel.OTHER.value,
}


def normalize_label(raw: str) -> str:
    label = raw.lower().strip().replace(" ", "_")
    label = LEGACY_LABEL_MAP.get(label, label)
    if label not in VALID_LABELS:
        return EmailLabel.OTHER.value
    return label


def _parse_label(content: str) -> tuple[str, float | None]:
    content = content.strip()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            return EmailLabel.OTHER.value, None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return EmailLabel.OTHER.value, None

    label = normalize_label(str(data.get("label", EmailLabel.OTHER.value)))
    confidence = data.get("confidence")
    try:
        confidence_f = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence_f = None
    return label, confidence_f


def _parse_interview_subtype(content: str) -> str | None:
    try:
        data = json.loads(content.strip())
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    subtype = data.get("interview_subtype")
    return subtype if isinstance(subtype, str) and subtype in VALID_INTERVIEW_SUBTYPES else None


def _combined_text(subject: str, sender: str, body_text: str, snippet: str) -> str:
    raw = f"{subject}\n{sender}\n{snippet}\n{body_text}".lower()
    return raw.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")


def _is_pre_interview_screen(text: str) -> bool:
    """Written screening for a named role — interview is not booked yet."""
    before_booking = any(
        re.search(p, text)
        for p in [
            r"\bbefore\s+(scheduling|we\s+schedule|setting\s+up|i\s+schedule)\s+"
            r"(a\s+)?(recruiter\s+|phone\s+|screening\s+)?interview\b",
            r"\bprior\s+to\s+(scheduling|an)\s+interview\b",
            r"\bbefore\s+(we\s+)?(move\s+forward\s+to|set\s+up)\s+(an\s+)?interview\b",
        ]
    )
    asks_written = any(
        s in text
        for s in [
            "reply with brief answers",
            "reply with answers",
            "answers to the questions",
            "answer the questions",
            "a few sentences per question",
            "could you reply with",
            "please answer the",
            "questions below",
        ]
    )
    thanks_interest = bool(re.search(r"\bthanks for your interest in\b", text))
    named_role = bool(re.search(r"\b(the\s+)?[\w /+-]+ (role|position) at\b", text))
    if before_booking and (asks_written or thanks_interest):
        return True
    return thanks_interest and named_role and asks_written


def _is_application_received(text: str) -> bool:
    """Post-apply receipt / 'we'll interview later if we want' — not a live invite."""
    if _looks_like_cold_jd_blast(text) or _is_true_rejection(text):
        return False
    receipts = [
        r"\bthank you for (taking the time to )?apply(ing)?\b",
        r"\bthanks for (taking the time to )?apply(ing)?\b",
        r"\bthank(s| you) for submitting your application\b",
        r"\bwe have received (your application|several applications|all of your materials)\b",
        r"\breceived (all of )?your (application|materials)\b",
        r"\byour application\b.{0,160}\bhas been received\b",
        r"\bapplication has been received\b",
        r"\bnow that we have your application\b",
        r"\bwe('ll| will) be in touch\b.*\bschedule an interview\b",
        r"\bif (we('d| would) like to|your qualifications meet)\b",
        r"\bone of our team members will get in touch\b",
        r"\bin the coming weeks to schedule an interview\b",
        r"\breview (them|applications) as quickly as possible\b",
    ]
    return any(re.search(p, text) for p in receipts)


def _is_hypothetical_interview(text: str) -> bool:
    return any(
        re.search(p, text)
        for p in [
            r"\bif we('d| would) like to schedule\b",
            r"\bif your qualifications meet\b",
            r"\bin the coming weeks to schedule an interview\b",
            r"\bmode of interview\b",
            r"\bjob description\b",
        ]
    )


def _is_true_interview_invite(text: str) -> bool:
    """Candidate-directed interview scheduling — not JD wording or screening Qs."""
    if _is_pre_interview_screen(text):
        return False
    if _is_application_received(text):
        return False
    if _looks_like_cold_jd_blast(text):
        return False
    if _is_hypothetical_interview(text) and not re.search(
        r"\b(confirmed for your interview|your interview (is|has been)|i('ll| will) call you)\b",
        text,
    ):
        return False
    patterns = [
        r"\byou are confirmed for your interview\b",
        r"\bconfirmed for your interview\b",
        r"\binvite[d]?\s+you\s+to\s+(an\s+)?interview\b",
        r"\bplease\s+(book|schedule)\s+(a|your)\s+(time|slot|interview)\b",
        r"\byour\s+interview\s+(is|has been|will be)\b",
        r"\binterview\s+(invitation|invite)\b",
        r"\bplease\s+(join|attend)\s+(the|your)\s+interview\b",
        r"\bcalendar\s+invite\b.*\binterview\b",
        r"\bi('ll| will) call you\b.*\binterview\b",
        r"\binterview\b.*\bi('ll| will) call you\b",
        r"\bbook\s+(a|your)\s+(time|slot)\b.*\binterview\b",
        r"\bselect (your|an?) interview (slot|time)\b",
        r"\b(interview|meeting)\b.{0,100}\b(scheduling link|choose a time|pick a slot)\b",
        r"\b(scheduling link|choose a time|pick a slot|book your interview)\b.{0,100}\binterview\b",
        r"\bbook your interview\b",
    ]
    return any(re.search(p, text) for p in patterns)


def _is_true_rejection(text: str) -> bool:
    patterns = [
        r"\bnot (?:be )?(?:able to )?mov(?:e|ing) forward\b",
        r"\bcannot move forward with your\b",
        r"\bregret to inform you\b",
        r"\byou have reached that limit\b",
        r"\breached (the|that|our) (application )?limit\b",
        r"\bwe have decided to move forward with other candidates?\b",
        r"\bunfortunately\b.*\b(your application|this role|this position|your candidacy)\b",
        r"\bwe\s+have\s+decided\s+to\s+move\s+forward\b",
        r"\bwill\s+not\s+be\s+progressing\b",
        r"\bnot been selected\b",
        r"\byou have not been selected to move forward\b",
        r"\bwe(?:['’]ve|\s+have) decided to move forward\b",
        r"\b(?:have\s+)?made the decision to move forward\b",
        r"\bmov(?:e|ing)\s+forward\s+with\s+(?:other\s+)?candidates\b",
        r"\bexperience aligns more directly\b",
        r"\bexperience more closely aligns\b",
        r"\baligns more directly to what this (?:specific )?role needs\b",
        r"\bunfortunately\b.{0,240}\bmove forward\b",
        r"\b(?:this )?(?:position|role|job) is not available in your (?:current )?location\b",
        r"\bnot available in your (?:current )?location\b",
        r"\bonly registered to hire\b",
        r"\b(?:we are|are only) registered to hire employees in specific locations\b",
        r"\bcannot hire (?:you )?in your (?:current )?location\b",
        r"\bnot (?:able|eligible) to hire (?:you )?(?:in|from) your (?:current )?location\b",
        r"\bproceed with (?:other\s+)?candidates\b",
        r"\bdecided to proceed with (?:other\s+)?candidates\b",
        r"\b(?:the|this) (?:role|position|job) (?:has been|is) filled\b",
        r"\b(?:the|this) (?:role|position|job) (?:has been|is) closed\b",
        r"\bbetter align with our current hiring needs\b",
    ]
    return any(re.search(p, text) for p in patterns)


def _is_true_screening(text: str) -> bool:
    """Booked recruiter/phone screen — not a technical/behavioral interview."""
    if _is_application_received(text) or _looks_like_cold_jd_blast(text):
        return False
    if _is_true_rejection(text):
        return False
    return any(
        re.search(p, text)
        for p in [
            r"\bphone\s+screen\b",
            r"\brecruiter\s+screen\b",
            r"\bscreening\s+call\b",
            r"\bschedule\s+a\s+(quick\s+)?(intro|introductory|recruiter)\s+call\b",
            r"\blet'?s\s+schedule\s+a\s+(quick\s+)?call\b",
        ]
    )


def _is_true_assessment(text: str) -> bool:
    patterns = [
        r"\bhackerrank\b",
        r"\bcodility\b",
        r"\btake[- ]home\b",
        r"\bcoding\s+test\b",
        r"\bonline\s+assessment\b",
        r"\bcomplete\s+(this|the)\s+assessment\b",
        r"\btechnical\s+assessment\b",
    ]
    return any(re.search(p, text) for p in patterns)


def _looks_like_cold_jd_blast(text: str) -> bool:
    """Cold recruiter JD pitch: role dump, even without 'send resume'."""
    jd_markers = [
        "job description",
        "job details",
        "job summary",
        "must have technical",
        "preferred skills",
        "preferred skills and knowledge",
        "mode of interview",
        "role:-",
        "experience required",
        "job type",
        "long term contract",
        "duration-",
        "duration -",
        "duration:",
    ]
    structure_markers = [
        "job description",
        "preferred skills",
        "mode of interview",
        "location",
        "duration",
        "summary:",
        "job –",
        "job-",
    ]
    ask_markers = [
        "send me a copy of your resume",
        "send your resume",
        "please send me a copy of your resume",
        "should you be interested",
        "if you are interested",
        "if interested",
        "share your resume",
        "forward your resume",
        "updated resume",
        "reply with your updated resume",
        "reply with your resume",
        "please reply with your",
        "find the requirement",
        "please find the requirement",
        "if you find yourself comfortable",
        "asap",
    ]
    sourcing_markers = [
        "unsubscribe",
        "posted your resume",
        "jobs portals",
        "job portals",
        "wish to be contacted",
        "email preferences",
        "technical it recruiter",
        "staffing",
    ]
    has_jd = sum(1 for m in jd_markers if m in text) >= 2
    has_structure = sum(1 for m in structure_markers if m in text) >= 3
    has_mode = "mode of interview" in text and ("job description" in text or "location" in text)
    has_ask = any(m in text for m in ask_markers)
    has_sourcing = any(m in text for m in sourcing_markers)
    has_req_fields = bool(
        re.search(r"\btitle\s*[-:]", text)
        and re.search(r"\blocation\s*[-:]", text)
        and re.search(r"\bduration\s*[-:]", text)
    )
    has_openings = bool(re.search(r"\b\d+\s+openings?\b", text))
    return (
        has_mode
        or has_structure
        or has_req_fields
        or (has_jd and (has_ask or has_sourcing or has_openings))
        or (has_ask and has_req_fields)
    )


def infer_interview_subtype(subject: str, sender: str, body_text: str, snippet: str) -> str:
    """Metadata for scheduled interviews; never a primary category."""
    text = _combined_text(subject, sender, body_text, snippet)
    if re.search(r"\b(interview|meeting)\b.{0,100}\b(cancelled|canceled)\b|\b(cancelled|canceled)\b.{0,100}\b(interview|meeting)\b", text):
        return "cancellation"
    if re.search(r"\b(reschedul\w*|moved to|new date)\b", text):
        return "reschedule"
    if re.search(r"\b(time change|different time|time has changed|updated time)\b", text):
        return "time_change"
    if re.search(r"\breminder\b|\binterview tomorrow\b", text):
        return "reminder"
    if re.search(r"\bcalendar (invitation|invite|event)\b|\binvitation from google calendar\b", text):
        return "calendar_invite"
    return "confirmation"


def _is_scheduled_interview(text: str) -> bool:
    if _looks_like_cold_jd_blast(text) or _is_application_received(text):
        return False
    return any(re.search(pattern, text) for pattern in (
        r"\b(your|the) interview (is|has been|was|will be) (scheduled|confirmed|moved|rescheduled|cancelled|canceled)\b",
        r"\bconfirmed for your interview\b",
        r"\binterview\b.{0,90}\b(on|at)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|\d{1,2}(:\d{2})?\s*(am|pm))\b",
        r"\b(calendar invite|calendar invitation|reminder|reschedule|time change|different time|cancelled|canceled)\b.{0,120}\binterview\b",
        r"\binterview\b.{0,120}\b(calendar invite|calendar invitation|reminder|reschedule|time change|different time|cancelled|canceled)\b",
        r"\binterview(?:er)?\b.{0,100}\b(time has changed|different time|updated time)\b",
    ))


def _is_application_action_required(text: str) -> bool:
    if not re.search(r"\b(application|candidate|profile|resume|cv|job portal|workday)\b", text):
        return False
    return any(re.search(pattern, text) for pattern in (
        r"\b(complete|finish|update) (your|the) (profile|application)\b",
        r"\b(upload|submit) (your|the|a) (resume|cv|missing (information|documents?))\b",
        r"\b(reset|set) (your|the) password\b",
        r"\b(missing information|action required|additional information required)\b",
    ))


def _is_interview_follow_up(text: str) -> bool:
    return any(re.search(pattern, text) for pattern in (
        r"\bthank you for (interviewing|meeting|speaking) with (us|our team)\b",
        r"\bfollow[- ]up (after|to|from) (your|the|our) interview\b",
        r"\b(post[- ]interview|interview feedback|feedback on your interview)\b",
        r"\bnext steps following (your|the) interview\b",
    ))


def _is_offer(text: str) -> bool:
    return any(re.search(pattern, text) for pattern in (
        r"\boffer letter\b",
        r"\b(pleased|excited|delighted) to offer you\b",
        r"\bcompensation (package|details|discussion)\b",
        r"\bnegotiat\w* (your |the )?offer\b",
    ))


def _heuristic_label(subject: str, sender: str, body_text: str, snippet: str) -> str | None:
    text = _combined_text(subject, sender, body_text, snippet)

    if _is_true_rejection(text):
        return EmailLabel.REJECTED_CLOSED.value
    if _is_offer(text):
        return EmailLabel.OFFER.value
    if _is_application_action_required(text):
        return EmailLabel.APPLICATION_ACTION_REQUIRED.value
    if _is_scheduled_interview(text):
        return EmailLabel.INTERVIEW_SCHEDULED.value
    if _is_interview_follow_up(text):
        return EmailLabel.INTERVIEW_FOLLOW_UP.value
    if _is_application_received(text):
        return EmailLabel.APPLICATION_CONFIRMATION.value
    if _looks_like_cold_jd_blast(text):
        return EmailLabel.RECRUITMENT_ALERT.value

    alert_signals = [
        "jobs that might interest you",
        "new jobs that might interest",
        "job alert",
        "job notification",
        "see all opportunities",
        "view all the jobs",
        "unsubscribe",
        "you have an interesting background",
        "fast track your application",
        "we are currently working with one of our top clients",
        "surely you may know someone",
        "browse our open positions",
        "find more opportunities",
        "posted your resume on jobs portals",
        "posted your resume on job portals",
        "wish to be contacted for job opportunities",
    ]
    if any(s in text for s in alert_signals):
        return EmailLabel.RECRUITMENT_ALERT.value

    if _is_true_assessment(text):
        return EmailLabel.ASSESSMENT.value
    if _is_pre_interview_screen(text) or _is_true_screening(text) or re.search(
        r"\b(recruiter questionnaire|screening questions|availability questions|chatbot questions)\b", text
    ):
        return EmailLabel.SCREENING.value
    if _is_true_interview_invite(text):
        return EmailLabel.INTERVIEW_INVITATION.value
    return None


def apply_label_guards(
    label: str,
    *,
    subject: str,
    sender: str,
    body_text: str,
    snippet: str,
) -> str:
    """Force known heuristic outcomes over a mistaken model label."""
    heuristic = _heuristic_label(subject, sender, body_text, snippet)
    text = _combined_text(subject, sender, body_text, snippet)
    if heuristic is not None:
        return heuristic
    if label in (EmailLabel.INTERVIEW_INVITATION.value, EmailLabel.INTERVIEW_SCHEDULED.value) and (
        _is_pre_interview_screen(text) or _is_true_screening(text)
    ):
        return EmailLabel.SCREENING.value
    return label


async def classify_email(
    *,
    subject: str,
    sender: str,
    body_text: str,
    snippet: str,
    force_openai: bool = False,
    use_openai: bool = True,
) -> ClassificationResult:
    heuristic = _heuristic_label(subject, sender, body_text, snippet)
    if has_saved_ai_settings():
        backend = get_ai_backend()
    else:
        settings = get_settings()
        backend = AiBackend("openai", settings.openai_model, settings.openai_api_key)

    # Heuristic-only path (fallback or tests). Live/Sync use OpenAI + guards.
    if not force_openai and not use_openai:
        return ClassificationResult(
            label=(heuristic or EmailLabel.OTHER.value),  # type: ignore[arg-type]
            confidence=0.55 if heuristic else 0.35,
            response_id=None,
        )
    if not backend.api_key:
        return ClassificationResult(
            label=(heuristic or EmailLabel.OTHER.value),  # type: ignore[arg-type]
            confidence=0.45 if heuristic else None,
            response_id=None,
        )

    client = backend.client() if backend.provider == "ollama" else AsyncOpenAI(api_key=backend.api_key)
    user_content = (
        f"From: {sender}\nSubject: {subject}\nSnippet: {snippet}\n\nBody:\n{body_text[:6000]}"
    )
    try:
        response = await client.chat.completions.create(
            model=backend.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": with_interview_subtype_rules(get_active_system_prompt())},
                {"role": "user", "content": user_content},
            ],
        )
    except Exception:
        logger.exception("%s classification failed", backend.provider)
        if backend.provider == "ollama":
            raise
        return ClassificationResult(
            label=(heuristic or EmailLabel.OTHER.value),  # type: ignore[arg-type]
            confidence=0.45 if heuristic else None,
            response_id=None,
        )
    content = response.choices[0].message.content or "{}"
    label, confidence = _parse_label(content)
    subtype = _parse_interview_subtype(content)
    label = apply_label_guards(
        label,
        subject=subject,
        sender=sender,
        body_text=body_text,
        snippet=snippet,
    )

    return ClassificationResult(
        label=label,  # type: ignore[arg-type]
        interview_subtype=(subtype or infer_interview_subtype(subject, sender, body_text, snippet)) if label == EmailLabel.INTERVIEW_SCHEDULED.value else None,
        confidence=confidence,
        response_id=response.id,
    )
