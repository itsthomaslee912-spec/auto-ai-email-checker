import type { EmailLabel, InterviewSubtype } from "./api";

export const INTERVIEW_SUBTYPE_TITLES: Record<InterviewSubtype, string> = {
  confirmation: "Confirmation",
  calendar_invite: "Calendar Invite",
  reminder: "Reminder",
  reschedule: "Reschedule",
  time_change: "Time Change",
  cancellation: "Cancellation",
};

export const CLASSIFY_LABELS: EmailLabel[] = [
  "application_confirmation",
  "application_action_required",
  "screening",
  "assessment",
  "interview_invitation",
  "interview_availability",
  "interview_scheduled",
  "interview_follow_up",
  "offer",
  "rejected_closed",
  "recruitment_alert",
  "other",
];

export const CLASSIFY_LABEL_TITLES: Record<EmailLabel, string> = {
  application_confirmation: "Application Confirmation",
  application_action_required: "Application Action Required",
  screening: "Screening",
  assessment: "Assessment",
  interview_invitation: "Interview Invitation",
  interview_availability: "Interview Availability",
  interview_scheduled: "Interview Scheduled",
  interview_follow_up: "Interview Follow-up",
  offer: "Offer",
  rejected_closed: "Rejected / Closed",
  recruitment_alert: "Recruitment Alert",
  other: "Other",
};

export const EMPTY_LABEL_COUNTS: Record<EmailLabel, number> = Object.fromEntries(
  CLASSIFY_LABELS.map((label) => [label, 0]),
) as Record<EmailLabel, number>;

export const LABEL_BAR_COLORS: Record<EmailLabel, string> = {
  application_confirmation: "var(--label-purple)",
  application_action_required: "var(--label-purple)",
  screening: "var(--label-mint)",
  assessment: "var(--warn)",
  interview_invitation: "var(--label-blue)",
  interview_availability: "var(--label-cyan)",
  interview_scheduled: "var(--label-blue)",
  interview_follow_up: "var(--label-cyan)",
  offer: "var(--label-green)",
  rejected_closed: "var(--danger)",
  recruitment_alert: "var(--label-orange)",
  other: "var(--muted)",
};
