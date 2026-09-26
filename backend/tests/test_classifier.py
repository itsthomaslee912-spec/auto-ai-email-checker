from app.classify.openai_classifier import apply_label_guards, _heuristic_label
from app.models import EmailLabel

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
