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
  application_confirmation: "#b7a6ff",
  application_action_required: "#e1adff",
  screening: "#8ee0b5",
  assessment: "#f0b429",
  interview_invitation: "#91b8ff",
  interview_scheduled: "#7ec8ff",
  interview_follow_up: "#6dc4cf",
  offer: "#5ecfc0",
  rejected_closed: "#e36a6a",
  recruitment_alert: "#ff9f43",
  other: "#8b95a7",
};
