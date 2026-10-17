from app.classify.openai_classifier import apply_label_guards, _heuristic_label, _is_scheduled_interview, _is_true_rejection, message_direction
from app.models import EmailLabel


def test_ada_accommodation_disclaimer_is_not_a_scheduled_interview_reminder():
    """"As a reminder, if you need accommodation... during the interview
    process" is a standard ADA disclaimer on application receipts, not a
    reference to an actual booked interview. "reminder" and "interview"
    landing within the heuristic's proximity window must not manufacture a
    false interview_scheduled/reminder label.
    """
    text = (
        "thank you for your application! we are excited that you're interested "
        "in the position of senior software engineer at example corp.\n\n"
        "what happens next? we'll review your application, and if there is a "
        "match we'll reach out to learn more.\n\n"
        "also, as a reminder, if you need accommodation, due to a disability, "
        "during any part of the interview process, please let us know so we "
        "can provide you with the best interview experience possible."
    )
    assert _is_scheduled_interview(text) is False
    assert _heuristic_label(
        "Interest in Example Corp!",
        "workday example <example@myworkday.com>",
        text,
        "",
    ) == EmailLabel.APPLICATION_CONFIRMATION.value


def test_should_we_move_forward_with_other_candidates_is_not_a_rejection():
    """"Should we decide to move forward with other candidates, we'll also let
    you know" is a conditional future outcome in an application receipt, not
    a present-tense rejection. Only "if/when/once" was previously recognized
    as a conditional trigger, so "should" slipped through and this receipt
    was misclassified as rejected_closed.
    """
    text = (
        "thank you for your interest in joining press ganey. we're pleased to confirm "
        "that we've received your application for the senior software engineer position.\n\n"
        "if your background aligns with our needs, we'll be in touch to discuss next steps. "
        "should we decide to move forward with other candidates, we'll also let you know."
    )
    assert _is_true_rejection(text) is False
    assert _heuristic_label(
        "Thank You For Applying - Press Ganey!",
        "Press Ganey <pressganey@myworkday.com>",
        text,
        "",
    ) == EmailLabel.APPLICATION_CONFIRMATION.value


def test_direction_and_latest_unquoted_text_control_classification():
    assert message_direction("sent", "Jordan <me@example.com>", "me@example.com") == ("outgoing", True)
    availability = "I am available Tuesday at 10 AM or Wednesday at 2 PM."
    assert _heuristic_label("Re: Interview", "me@example.com", availability, "", direction="outgoing") == EmailLabel.INTERVIEW_AVAILABILITY.value

    body = (
        "Thanks, this is already complete.\n\n"
        "On Monday, Recruiter wrote:\n"
        "Action required: upload your missing resume to continue your application."
    )
    assert _heuristic_label("Re: Application", "me@example.com", body, "", direction="outgoing") is None
    assert _heuristic_label(
        "Coding challenge",
        "Assessments <noreply@example.com>",
        "Please complete your coding challenge by Friday.",
        "",
    ) == EmailLabel.ASSESSMENT.value
    wrapped_rejection = (
        "Thank you for taking the time to discuss the role. "
        "The hiring leader has decided to pursue\n      other candidates for this position."
    )
    assert _heuristic_label(
        "Update on your application",
        "Recruiting Team <recruiting@example.com>",
        wrapped_rejection,
        "",
    ) == EmailLabel.REJECTED_CLOSED.value


def test_incoming_mail_cannot_be_candidate_interview_availability():
    assert apply_label_guards(
        EmailLabel.INTERVIEW_AVAILABILITY.value,
        subject="Re: Interview availability",
        sender="Recruiter <recruiter@example.com>",
        body_text=(
            "HR is waiting for your response to some of their questions. Do you need more time? "
            "I would be happy to schedule a chat if helpful."
        ),
        snippet="",
        direction="incoming",
    ) == EmailLabel.SCREENING.value

BLOCKSTREAM = """Hi Thomas,

Thank you so much for your interest in joining Blockstream! We have received several applications for the Software Engineer, Enterprise Custody role and are doing our best to review them as quickly as possible.

If your qualifications meet our needs, one of our team members will get in touch with you in the coming weeks to schedule an interview.

Thank you again for your interest in our company. We really appreciate the time you invested in this application.

All the best,
Blockstream Hiring Team
"""

RULES_IQ = """Hi ,

Job –                  Datacenter Technician

Location –:       Remote (Must be within 45 minutes of Switch location in Lithia Springs, Georgia)

Duration- :       Long term contract

Mode of Interview: MS Teams with possible 2nd round face-to-face interview in Lithia Springs, GA

Job Description:

Summary:

To assist in managing the efficiency and optimal performance of the Lithia Springs and remote data centers.

Preferred Skills and Knowledge:

Advanced knowledge of servers, network cabling, and data center hardware

Atul Singh
Sr. Technical It Recruiter | Rules IQ
"""

KATE = """Hi Thomas,

You are confirmed for your interview with me on Wednesday September 16, 1:00pm (GMT-04:00) Eastern Time (US & Canada). I'll call you at +16789120809.

If you have any questions or need to reschedule, don't hesitate to reach out. I'm looking forward to speaking with you!

Thanks,

Kate
"""

SAGENT = """Hi Thomas!

Thanks for taking the time to apply to our Software Development Engineer SR -Full Stack Java/React role. We’re thrilled that you’d like to join us here at Sagent.

Now that we have your application, we’ll be in touch in the short term if we’d like to schedule an interview. Unfortunately, the days are short and the applicants are many so we won’t have time to meet everyone. If we don’t feel we’re a great match, we will let you know right away.

Again, thanks for your interest in Sagent!

Regards,
Sagent Talent Team
"""


def test_blockstream_is_applied():
    assert _heuristic_label("", "Blockstream", BLOCKSTREAM, "") == EmailLabel.APPLICATION_CONFIRMATION.value
    assert (
        apply_label_guards(EmailLabel.INTERVIEW_SCHEDULED.value, subject="", sender="", body_text=BLOCKSTREAM, snippet="")
        == EmailLabel.APPLICATION_CONFIRMATION.value
    )


def test_sagent_is_applied():
    assert _heuristic_label("", "Sagent", SAGENT, "") == EmailLabel.APPLICATION_CONFIRMATION.value
    assert (
        apply_label_guards(EmailLabel.INTERVIEW_SCHEDULED.value, subject="", sender="", body_text=SAGENT, snippet="")
        == EmailLabel.APPLICATION_CONFIRMATION.value
    )


def test_rules_iq_jd_is_alert():
    assert _heuristic_label("Datacenter Technician", "Atul Singh", RULES_IQ, "") == EmailLabel.RECRUITMENT_ALERT.value
    assert (
        apply_label_guards(EmailLabel.INTERVIEW_SCHEDULED.value, subject="", sender="", body_text=RULES_IQ, snippet="")
        == EmailLabel.RECRUITMENT_ALERT.value
    )


def test_kate_confirmation_is_interview():
    assert _heuristic_label("", "Kate", KATE, "") == EmailLabel.INTERVIEW_SCHEDULED.value
    assert (
        apply_label_guards(EmailLabel.INTERVIEW_SCHEDULED.value, subject="", sender="", body_text=KATE, snippet="")
        == EmailLabel.INTERVIEW_SCHEDULED.value
    )


UPSTART = """Hi Thomas Lee,

Thank you for your interest in opportunities at Upstart and for taking the time to apply.

We recommend applying to a small number of roles that closely match your experience and interests. Please note that candidates may apply to up to three roles within a 60 day period. At this time, you have reached that limit, so we will not be able to move forward with your most recent application.

You are welcome to reapply after the 60 day window has passed, and we encourage you to consider roles that closely align with your background.

We appreciate your interest in Upstart and wish you the best in your job search.

Best regards,
Upstart Recruiting Team
"""


def test_upstart_application_limit_is_rejected():
    assert _heuristic_label("", "Upstart", UPSTART, "") == EmailLabel.REJECTED_CLOSED.value
    assert (
        apply_label_guards(EmailLabel.OTHER.value, subject="", sender="", body_text=UPSTART, snippet="")
        == EmailLabel.REJECTED_CLOSED.value
    )


STACKADAPT = """Hi Thomas,

Thank you for taking the time to apply for the Senior Quality Engineer position at StackAdapt. After carefully reviewing your application, we regret to inform you that we will not be moving forward with your application at this time.

Due to the high volume of applications we received, we are unable to provide individual feedback. However, we greatly appreciate your interest in our company and recognize the effort you put into your application.

We encourage you to keep an eye on our careers site for future opportunities that may be a great fit for you.

Best,

Talent Acquisition Team

StackAdapt
"""


def test_stackadapt_not_moving_forward_is_rejected():
    assert _heuristic_label("", "StackAdapt", STACKADAPT, "") == EmailLabel.REJECTED_CLOSED.value
    assert (
        apply_label_guards(EmailLabel.APPLICATION_CONFIRMATION.value, subject="", sender="", body_text=STACKADAPT, snippet="")
        == EmailLabel.REJECTED_CLOSED.value
    )


MOTLEY_FOOL = """Dear Thomas,

Thank you for submitting your application! We have received all of your materials for Product Tech Lead.

Here at The Motley Fool, we strive to not only make the company a great place to work, but we also want to make the application process fun and enjoyable, which means you don’t have to worry about your application being sucked into a black hole. We’ve got it; we promise!

Someone from our recruiting team will reach out when your application is processed.

Sincerely,
The Motley Fool Recruiting Team

** Please note: Do not reply to this email. This email is sent from an unattended mailbox. Replies will not be read.
"""

STRIIM = """Hello Thomas,

Thanks for applying to Striim, Inc.. Your application has been received, and we will review it right away.

If your application seems like a good fit for the position we will contact you soon.

Regards,
Striim, Inc.

** Please note: Do not reply to this email. This email is sent from an unattended mailbox. Replies will not be read.
"""

DATABRICKS = """Hi Thomas,

Thanks for applying to Databricks! Your application for the Sr. Solutions Engineer - Digital Native Business, Named Accounts role has been received. We will review it shortly and reach out if there is a fit.

Please note that all official communication from Databricks will come from email addresses ending with @databricks.com or @goodtime.io (our meeting tool).

Regards,
Databricks

** Please note: Do not reply to this email. This email is sent from an unattended mailbox. Replies will not be read.
"""

CMT_RECEIPT = """Hi Jordan,

Thank you for applying to Cambridge Mobile Telematics!

Your application has been received and will be reviewed as soon as possible. If your background is a fit, we will reach out to set up a call.

Best regards,
CMT Talent Team
"""

CAMBLY_RECEIPT = """Thank you for applying. Our recruiting team is reviewing applications and plans to schedule interviews in the coming weeks. If you are qualified, a recruiter will contact you to schedule a phone call."""


def test_motley_fool_receipt_is_applied():
    assert _heuristic_label("", "The Motley Fool", MOTLEY_FOOL, "") == EmailLabel.APPLICATION_CONFIRMATION.value
    assert (
        apply_label_guards(EmailLabel.OTHER.value, subject="", sender="", body_text=MOTLEY_FOOL, snippet="")
        == EmailLabel.APPLICATION_CONFIRMATION.value
    )


def test_striim_receipt_is_applied():
    assert _heuristic_label("", "Striim", STRIIM, "") == EmailLabel.APPLICATION_CONFIRMATION.value
    assert (
        apply_label_guards(EmailLabel.OTHER.value, subject="", sender="", body_text=STRIIM, snippet="")
        == EmailLabel.APPLICATION_CONFIRMATION.value
    )


def test_databricks_receipt_is_applied():
    assert _heuristic_label("", "Databricks", DATABRICKS, "") == EmailLabel.APPLICATION_CONFIRMATION.value
    assert (
        apply_label_guards(EmailLabel.OTHER.value, subject="", sender="", body_text=DATABRICKS, snippet="")
        == EmailLabel.APPLICATION_CONFIRMATION.value
    )


def test_conditional_future_calls_remain_application_confirmations():
    for body in (CMT_RECEIPT, CAMBLY_RECEIPT):
        assert _heuristic_label("", "", body, "") == EmailLabel.APPLICATION_CONFIRMATION.value
        assert (
            apply_label_guards(
                EmailLabel.INTERVIEW_INVITATION.value,
                subject="",
                sender="",
                body_text=body,
                snippet="",
            )
            == EmailLabel.APPLICATION_CONFIRMATION.value
        )


def test_application_receipt_with_direct_booking_request_is_an_invitation():
    body = """We received your application and would like to interview you. Please use this scheduling link to choose a time for your interview."""
    assert _heuristic_label("", "", body, "") == EmailLabel.INTERVIEW_INVITATION.value


def test_encoded_or_damaged_contractions_do_not_hide_rejections():
    bodies = (
        "Thank you for applying. Unfortunately, we won&rsquo;t be moving forward with your candidacy.",
        "Thank you for applying. Unfortunately, you weren\ufffdt selected for further consideration.",
    )
    for body in bodies:
        assert _heuristic_label("", "", body, "") == EmailLabel.REJECTED_CLOSED.value


def test_explicit_closed_or_paused_application_overrides_receipt_language():
    bodies = (
        "Thank you for applying. We are unable to move forward with your application at this time.",
        "Thank you for applying. Unfortunately, we no longer have any openings for this role.",
        "Thank you for applying. We have decided to pause this search temporarily while we reassess the role.",
        "Thank you for applying. At this time, the client is only considering candidates who are currently local.",
    )
    for body in bodies:
        assert _heuristic_label("", "", body, "") == EmailLabel.REJECTED_CLOSED.value


def test_job_digest_footer_actions_do_not_become_application_actions():
    body = """You joined the AGFA Talent Community. Your Job Agent matched the following jobs at careers.agfa.com. Update your preferences or reset your password to access your profile and agents."""
    assert _heuristic_label("New jobs posted from careers.agfa.com", "", body, "") == EmailLabel.RECRUITMENT_ALERT.value


CLOUDBEDS = """Hi Jordan,

Thank you for applying for the Senior Software Engineer - Workflow role. We genuinely appreciate the time you invested in your application. After carefully reviewing your background, we've decided to move forward with candidates whose experience aligns more directly to what this specific role needs.

We're growing fast, and new roles open up regularly. When you see something that feels like a better match, we genuinely want to hear from you again. Keep an eye on Cloudbeds Careers and follow our LinkedIn  page to stay informed about future opportunities.

Thank you again for considering Cloudbeds. We're wishing you the best in your search.

Best Regards,

The Talent Acquisition Team @ Cloudbeds

Please note: per company policy, we are unable to provide individual application feedback to candidates at this stage in the process.
"""


def test_cloudbeds_is_rejected():
    assert _heuristic_label("", "Cloudbeds", CLOUDBEDS, "") == EmailLabel.REJECTED_CLOSED.value
    assert (
        apply_label_guards(EmailLabel.APPLICATION_CONFIRMATION.value, subject="", sender="", body_text=CLOUDBEDS, snippet="")
        == EmailLabel.REJECTED_CLOSED.value
    )


PRINCIPAL_REQUIREMENT = """Hi,

Please find the requirement below, if you find yourself comfortable with the requirement please reply with your updated resume and I will get back to you.

Title- Sr. Software Engineer
Location- Remote or Hybrid Des Moines, IA
Duration – 6+ Month
Interview – Video

Client - Principal Financial

They are looking for more of a Software Engineer/Doer in this space.  Their job description is awful and they’ve titled it a Sr. Software Engineer.
Below is the job description, I actually have 3 openings.

Process Account Password Changes

This effort requires changing passwords for process accounts and updating each associated application's configuration to use the new credentials.  A process account is simply a non-human account that is used to run processes, batch jobs, etc.  In our current state, we have many shared accounts that are used across multiple applications, and part of this effort will be to break those into individual dedicated accounts.   The new accounts will need to be created and each app reconfigured to use its own.  This role will be given patterns to follow to complete the work and be accountable to make the changes and prepare pull requests for the owning teams to approve.
"""


def test_requirement_dump_is_alert():
    assert _heuristic_label("", "Recruiter", PRINCIPAL_REQUIREMENT, "") == EmailLabel.RECRUITMENT_ALERT.value
    assert (
        apply_label_guards(EmailLabel.SCREENING.value, subject="", sender="", body_text=PRINCIPAL_REQUIREMENT, snippet="")
        == EmailLabel.RECRUITMENT_ALERT.value
    )


EVERPURE = """Hi Thomas,

Thank you so much for taking the time to apply for the Senior Full Stack Software Engineer, DX role. We know a lot of thought and consideration went into your application, and we genuinely appreciate your interest in joining the team here at Everpure (formerly Pure Storage). Unfortunately, we have made the decision to move forward with other candidates whose experience more closely aligns with our team's needs.

Thanks again for your interest in Everpure (formerly Pure Storage)! We wish you the best of luck in your current search.

Regards,

The Recruiting Team at Everpure (formerly Pure Storage)
"""


def test_everpure_is_rejected():
    assert _heuristic_label("", "Everpure", EVERPURE, "") == EmailLabel.REJECTED_CLOSED.value
    assert (
        apply_label_guards(EmailLabel.OTHER.value, subject="", sender="", body_text=EVERPURE, snippet="")
        == EmailLabel.REJECTED_CLOSED.value
    )


CLEARLINK = """Hello Thomas,

Thank you for your interest in the Senior Software Engineer, Wordpress position here at Clearlink. At this time, this position is not available in your current location. If the position you applied for is listed as remote, please note that Clearlink is only registered to hire employees in specific locations, regardless of if the position is remote, hybrid, or in-office.

We encourage you to keep an eye out for future opportunities with us; follow us on social media and check out our careers page for more opportunities - we post new ones regularly!

Thank you for considering us as part of your journey.

Kind regards,

Clearlink Recruiting
"""


def test_clearlink_location_is_rejected():
    assert _heuristic_label("", "Clearlink", CLEARLINK, "") == EmailLabel.REJECTED_CLOSED.value
    assert (
        apply_label_guards(EmailLabel.APPLICATION_CONFIRMATION.value, subject="", sender="", body_text=CLEARLINK, snippet="")
        == EmailLabel.REJECTED_CLOSED.value
    )


CROSSCOUNTRY = """Hi Thomas,
Thank you for your interest in CrossCountry Consulting and for considering us as a possible next step in your career.

After reviewing your application, our team has decided to proceed with other candidates who better align with our current hiring needs. We will contact you for future opportunities based on changes in the market or shifts in our hiring needs.

We wish you all the best in your job search and professional endeavors.

Best,
The CrossCountry Consulting Talent Team
LinkedIn| Facebook | Glassdoor
"""


def test_crosscountry_is_rejected():
    assert _heuristic_label("", "CrossCountry", CROSSCOUNTRY, "") == EmailLabel.REJECTED_CLOSED.value
    assert (
        apply_label_guards(EmailLabel.OTHER.value, subject="", sender="", body_text=CROSSCOUNTRY, snippet="")
        == EmailLabel.REJECTED_CLOSED.value
    )


def test_system_prompt_lists_every_label():
    from app.classify.openai_classifier import normalize_label
    from app.classify.prompt import CLASSIFY_TYPES, SYSTEM_PROMPT

    slugs = {item.value for item in EmailLabel}
    assert {item.slug for item in CLASSIFY_TYPES} == slugs
    assert "unknown" not in slugs
    for slug in slugs:
        assert slug in SYSTEM_PROMPT
    assert "- unknown" not in SYSTEM_PROMPT
    assert normalize_label("applied") == EmailLabel.APPLICATION_CONFIRMATION.value
    assert normalize_label("application_submitted") == EmailLabel.APPLICATION_CONFIRMATION.value
    assert normalize_label("alert") == EmailLabel.RECRUITMENT_ALERT.value
    assert normalize_label("available") == EmailLabel.INTERVIEW_INVITATION.value
    assert normalize_label("tech") == EmailLabel.ASSESSMENT.value
    assert normalize_label("unknown") == EmailLabel.OTHER.value
    assert normalize_label("not_a_label") == EmailLabel.OTHER.value


def test_runtime_prompt_removes_legacy_static_fallback_examples():
    from app.classify.prompt import SYSTEM_PROMPT, with_interview_subtype_rules

    legacy = (
        SYSTEM_PROMPT
        + "\n\nAdditional rules learned from training:\n"
        + "Human-labeled examples. Infer intent.\n- Example 1: correct label=other."
    )
    upgraded = with_interview_subtype_rules(legacy)
    assert "Example 1" not in upgraded
    assert '"facts"' in upgraded


def test_runtime_prompt_adds_conditional_interview_rule_to_saved_prompt():
    from app.classify.prompt import with_interview_subtype_rules

    upgraded = with_interview_subtype_rules("Classify recruiting email. interview_subtype facts category_subtype")
    assert "Future or conditional interview language" in upgraded


def test_booked_calendar_request_is_scheduled_and_a_decline_is_not():
    booked = """BEGIN:VCALENDAR
METHOD:REQUEST
BEGIN:VEVENT
DTSTART;TZID=America/New_York:20260922T123000
DTEND;TZID=America/New_York:20260922T130000
END:VEVENT
END:VCALENDAR
"""
    declined = """BEGIN:VCALENDAR
METHOD:REPLY
BEGIN:VEVENT
DTSTART;TZID=America/New_York:20260831T080000
STATUS:CONFIRMED
END:VEVENT
END:VCALENDAR
Please reply with your availability.
"""
    assert (
        apply_label_guards(
            EmailLabel.INTERVIEW_INVITATION.value,
            subject="Book time with me - Ryan Queen and Mathius Ballesteros Pacheco",
            sender="Mathius <mathius@synply.io>",
            body_text=booked,
            snippet="",
        )
        == EmailLabel.INTERVIEW_SCHEDULED.value
    )
    assert (
        apply_label_guards(
            EmailLabel.INTERVIEW_INVITATION.value,
            subject="Declined: Fivecast intro call: AI Engineer",
            sender="Nicole <nicole@fivecast.test>",
            body_text=declined,
            snippet="",
        )
        == EmailLabel.INTERVIEW_INVITATION.value
    )
    assert (
        apply_label_guards(
            EmailLabel.REJECTED_CLOSED.value,
            subject="Declined: Kenneth and Tremayne Matthews",
            sender="kchung121@outlook.com",
            body_text="",
            snippet="",
        )
        == EmailLabel.INTERVIEW_INVITATION.value
    )
    assert (
        apply_label_guards(
            EmailLabel.INTERVIEW_INVITATION.value,
            subject="Interview invitation: Principal AI Engineer",
            sender="noreply@taleri.es",
            body_text="https://scheduler.zoom.us/mathius/hello",
            snippet="",
        )
        == EmailLabel.INTERVIEW_INVITATION.value
    )


def test_offer_requires_an_actual_candidate_offer_not_job_description_boilerplate():
    assert _heuristic_label(
        "Incomplete job application",
        "Workday",
        "Your application has been removed because it was not completed within 14 days. "
        "You may reapply. Our company offers a competitive compensation package.",
        "",
    ) == EmailLabel.REJECTED_CLOSED.value

    assert _heuristic_label(
        "Re: Recruiter questions",
        "Candidate",
        "Please see my responses below.\n\nOn Monday, Recruiter wrote:\n"
        "The position includes a competitive compensation package.",
        "",
    ) == EmailLabel.SCREENING.value

    application_receipts = (
        "If your application is successful, your job offer letter will be sent to your account.",
        "After the interviews, we will extend an offer to shortlisted candidates.",
        "Never share bank details before you've received and signed any formal offer.",
        "Learn about the process from applying through a potential offer of employment.",
    )
    for body in application_receipts:
        assert _heuristic_label(
            "Thank you for applying",
            "Talent",
            f"We received your application. {body}",
            "",
        ) == EmailLabel.APPLICATION_CONFIRMATION.value


def test_future_rejection_process_language_does_not_close_an_application():
    cases = (
        "If you haven't heard from us within 30 days, it likely means we've decided to move forward with other candidates.",
        "Once the role is filled or closed, we'll send a final update.",
        "We will inform you if/when the position is filled.",
    )
    for sentence in cases:
        body = f"Thank you for applying. We received your application. {sentence}"
        assert _heuristic_label("Application received", "Talent", body, "") == EmailLabel.APPLICATION_CONFIRMATION.value


def test_candidate_account_mentioned_in_receipt_is_not_action_required():
    body = "We received your application. You may access your candidate account to view your application status."
    assert _heuristic_label("Thank you for applying", "Talent", body, "") == EmailLabel.APPLICATION_CONFIRMATION.value


def test_direct_recruiter_questions_override_application_thank_you():
    cases = (
        (
            "Thank you for applying. Could you let me know if you would relocate? "
            "If so, what timeframe would work best for you?"
        ),
        (
            "Thank you for applying. The hiring manager has asked me to reach out to you "
            "to get a better understanding of your experience. Have you built a local LLM? "
            "What's your experience with local LLM? Please explain."
        ),
    )
    for body in cases:
        assert _heuristic_label("Next step", "Recruiter", body, "") == EmailLabel.SCREENING.value


def test_async_video_screen_is_screening_not_application_confirmation():
    body = (
        "After reviewing your application, you're moving forward in our hiring process. "
        "Please submit a short video to introduce yourself that covers the following questions."
    )
    assert _heuristic_label("Next step AI interview", "Recruiter", body, "") == EmailLabel.SCREENING.value

    reminder = "Your application went through. The AI interview is the step that is left. Take your AI interview now."
    assert _heuristic_label("Last reminder: your AI interview", "Recruiter", reminder, "") == EmailLabel.SCREENING.value


def test_latest_availability_request_beats_quoted_old_confirmation():
    body = """Can you provide me with your availability this week?

From: Recruiter
Sent: Friday, October 2, 2026
Subject: Earlier confirmation
Your video interview has been confirmed on Friday at 3:00 PM.
"""
    assert _heuristic_label("Re: Interview", "Recruiter", body, "") == EmailLabel.INTERVIEW_INVITATION.value


def test_visible_snippet_beats_leading_html_css_for_async_screen():
    body = "body { width: 600px; } " * 400
    snippet = "Final reminder: complete your AI interview to continue your application."
    assert _heuristic_label("Final reminder", "Recruiter", body, snippet) == EmailLabel.SCREENING.value


def test_offer_recognizes_offer_letter_acceptance_and_post_offer_onboarding():
    cases = (
        ("Your offer letter", "We are pleased to offer you the Engineer role."),
        ("Offer to join Acme - Signature requested", "Please review and sign your offer."),
        ("Background verification", "You have accepted our offer. Please begin the background verification."),
        ("Welcome to Acme", "Please complete your pre-hire onboarding tasks before your start date."),
    )
    for subject, body in cases:
        assert _heuristic_label(subject, "Talent", body, "") == EmailLabel.OFFER.value


def test_offer_ignores_negated_eeo_disclaimer_boilerplate():
    """"This email is not an offer of employment" is standard EEO boilerplate
    appended to plain application receipts — it must not force the offer label."""
    cases = (
        (
            "Wells Fargo Careers: Thank you for applying",
            "Dear Thomas, thank you for your interest in Wells Fargo. We have received "
            "your application for the following position: R-574194 Lead Infrastructure Engineer. "
            "Note: this email is not an offer of employment.",
        ),
        (
            "You're invited to Specific Specialists on Slack",
            "Hi Thomas, thanks for applying for the role through our marketplace. "
            "Join the Slack workspace. Joining the workspace does not constitute an offer "
            "of employment or a contractor engagement.",
        ),
    )
    for subject, body in cases:
        assert _heuristic_label(subject, "Talent", body, "") != EmailLabel.OFFER.value


def test_offer_requires_job_context_for_excited_to_offer_you():
    """"We're excited to offer you X% off" is marketing, not a job offer."""
    assert _heuristic_label(
        "Thanks for being a valued member",
        "LinkedIn",
        "Hi Thomas, we're excited to offer you an exclusive free trial and then "
        "25% off your premium career subscription - for life.",
        "",
    ) != EmailLabel.OFFER.value


def test_offer_requires_start_date_for_standalone_onboarding_mention():
    """A standalone onboarding-tasks mention with no job context is not an offer."""
    assert _heuristic_label(
        "Your Slack onboarding tasks are waiting",
        "Slack",
        "Get set up and ready to collaborate with your team.",
        "",
    ) != EmailLabel.OFFER.value


def test_quoted_offer_language_does_not_override_latest_message():
    assert _heuristic_label(
        "Re: Senior Engineer opportunity",
        "Candidate",
        "Thanks, I am interested in learning more.\n\nOn Friday, Recruiter wrote:\n"
        "This job description includes compensation details and an offer letter process.",
        "",
    ) != EmailLabel.OFFER.value

    assert _heuristic_label(
        "Re: Permanent position offer for Jordan",
        "Candidate",
        "I am interested in this opportunity.\n\nSent with Spark\nOn Aug 31, Recruiter wrote:",
        "",
    ) == EmailLabel.RECRUITMENT_ALERT.value
