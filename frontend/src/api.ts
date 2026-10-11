export type EmailLabel =
  | "application_confirmation"
  | "application_action_required"
  | "screening"
  | "assessment"
  | "interview_invitation"
  | "interview_availability"
  | "interview_scheduled"
  | "interview_follow_up"
  | "offer"
  | "rejected_closed"
  | "recruitment_alert"
  | "other";

export type MailFolder = "inbox" | "sent" | "spam" | "trash" | "archive";
export type InterviewSubtype = "confirmation" | "calendar_invite" | "reminder" | "reschedule" | "time_change" | "cancellation";

export type Provider = "google" | "microsoft";

export interface Mailbox {
  id: number;
  provider: Provider;
  email_address: string;
  display_name: string | null;
  calendar_color?: string | null;
  is_active: boolean;
  created_at: string | null;
}

export type AutoSyncState = "active" | "syncing" | "no_account" | "stopped" | "error";

export interface AutoSyncStatus {
  state: AutoSyncState;
  interval_seconds: number;
  connected_accounts: number;
  webhook_accounts: number;
  registered_webhook_accounts?: number;
  polling_accounts?: number;
  webhook_delivery?: Record<string, {
    healthy: boolean;
    last_success_at: string | null;
    last_failure_at: string | null;
    last_error: string | null;
  }>;
  ngrok?: {
    state: "online" | "offline" | "mismatch";
    public_url: string | null;
    expected_url: string | null;
  };
  last_check_at: string | null;
  last_error: string | null;
  problem_mailboxes: { mailbox_id: number; email_address: string; message: string }[];
  mailboxes?: AccountSyncDetail[];
}

export interface AccountSyncDetail {
  mailbox_id: number;
  email_address: string;
  provider: Provider;
  state: string;
  last_sync_started_at: string | null;
  last_sync_completed_at: string | null;
  last_error: string | null;
  needs_reconnect?: boolean;
  webhook_registered: boolean;
  webhook_verified: boolean;
  sync_method?: "webhook" | "polling";
  polling_reason?: string | null;
  last_webhook_success_at?: string | null;
}

export interface EmailItem {
  id: number;
  mailbox_id: number;
  provider_message_id: string;
  subject: string;
  sender: string;
  received_at: string | null;
  snippet: string;
  label: EmailLabel;
  interview_subtype: InterviewSubtype | null;
  confidence: number | null;
  is_read: boolean;
  human_corrected?: boolean;
  folder?: MailFolder;
  thread_id?: string | null;
  thread_count?: number;
  created_at: string | null;
}

export interface EmailDetail extends EmailItem {
  body_text: string;
  body_html?: string;
}

const API_BASE = import.meta.env.VITE_API_BASE ?? "";

function friendlyNetworkError(err: unknown, fallback: string): Error {
  const message = err instanceof Error ? err.message : "";
  if (/failed to fetch|networkerror|load failed|econnrefused/i.test(message)) {
    return new Error("Cannot reach the API. Sync may still be running in the background — retry in a moment.");
  }
  return err instanceof Error ? err : new Error(fallback);
}

async function readApiError(res: Response, fallback: string): Promise<string> {
  try {
    const data = await res.json();
    if (typeof data?.detail === "string") return data.detail;
    if (Array.isArray(data?.detail)) {
      return data.detail.map((d: { msg?: string }) => d.msg).filter(Boolean).join("; ") || fallback;
    }
  } catch {
    /* ignore */
  }
  return `${fallback} (${res.status})`;
}

export const AUTH_EXPIRED_EVENT = "email-checker:auth-expired";

function noteUnauthorized(res: Response): Response {
  if (res.status === 401 && !res.url.includes("/api/session/")) {
    window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
  }
  return res;
}

async function apiFetch(url: string, init?: RequestInit, attempts = 3): Promise<Response> {
  let lastError: unknown = null;
  for (let i = 0; i < attempts; i += 1) {
    try {
      const res = await fetch(url, { credentials: "include", ...init });
      return noteUnauthorized(res);
    } catch (err) {
      lastError = err;
      await new Promise((resolve) => window.setTimeout(resolve, 350 * (i + 1)));
    }
  }
  throw friendlyNetworkError(lastError, "Cannot reach the API");
}

export async function fetchMailboxes(): Promise<Mailbox[]> {
  const res = await apiFetch(`${API_BASE}/api/mailboxes`);
  if (!res.ok) throw new Error(await readApiError(res, "Failed to load mailboxes"));
  return res.json();
}

export async function updateMailboxColor(id: number, calendarColor: string): Promise<Mailbox> {
  const res = await apiFetch(`${API_BASE}/api/mailboxes/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ calendar_color: calendarColor }),
  });
  if (!res.ok) throw new Error(await readApiError(res, "Failed to save account color"));
  return res.json();
}

async function autoSyncRequest(method: "GET" | "POST", path: string): Promise<AutoSyncStatus> {
  const controller = new AbortController();
  // Sync status can stall briefly while the backend is classifying or importing mail.
  const timeout = window.setTimeout(() => controller.abort(), 15_000);
  try {
    const res = noteUnauthorized(await fetch(`${API_BASE}${path}`, {
      method,
      signal: controller.signal,
      cache: "no-store",
      credentials: "include",
    }));
    if (!res.ok) throw new Error(await readApiError(res, "Failed to check automatic sync"));
    return res.json();
  } finally {
    window.clearTimeout(timeout);
  }
}

export function fetchAutoSyncStatus(): Promise<AutoSyncStatus> {
  return autoSyncRequest("GET", "/api/auto-sync/status");
}

export function checkAutoSyncNow(): Promise<AutoSyncStatus> {
  return autoSyncRequest("POST", "/api/auto-sync/check");
}

export interface EmailPage {
  items: EmailItem[];
  next_cursor: number | null;
  next_received_at: string | null;
  next_is_read?: boolean | null;
  has_more: boolean;
  total: number;
  label_counts: Record<string, number>;
  mailbox_counts: Record<string, number>;
  mailbox_unread_counts: Record<string, number>;
  folder_counts?: Record<string, number>;
  /** [mailbox_id, label, folder, unread, count] cells across all active mailboxes. */
  count_breakdown?: [number, string, string, boolean, number][];
}

export async function fetchEmails(opts?: {
  label?: string;
  interviewSubtype?: InterviewSubtype | null;
  folder?: string;
  mailboxId?: number | null;
  query?: string | null;
  inboxType?: "default" | "unread_first";
  conversation?: boolean;
  limit?: number;
  beforeId?: number | null;
  beforeReceivedAt?: string | null;
  beforeIsRead?: boolean | null;
}): Promise<EmailPage> {
  const params = new URLSearchParams();
  if (opts?.label && opts.label !== "all") params.set("label", opts.label);
  if (opts?.interviewSubtype) params.set("interview_subtype", opts.interviewSubtype);
  if (opts?.folder && opts.folder !== "all") params.set("folder", opts.folder);
  if (opts?.query?.trim()) params.set("q", opts.query.trim());
  if (opts?.mailboxId != null) params.set("mailbox_id", String(opts.mailboxId));
  if (opts?.inboxType && opts.inboxType !== "default") params.set("inbox_type", opts.inboxType);
  if (opts?.conversation) params.set("conversation", "true");
  params.set("limit", String(opts?.limit ?? 50));
  if (opts?.beforeId != null) params.set("before_id", String(opts.beforeId));
  if (opts?.beforeReceivedAt) params.set("before_received_at", opts.beforeReceivedAt);
  if (opts?.beforeIsRead != null) params.set("before_is_read", String(opts.beforeIsRead));
  const qs = params.toString();
  const res = await apiFetch(`${API_BASE}/api/emails${qs ? `?${qs}` : ""}`);
  if (!res.ok) throw new Error(await readApiError(res, "Failed to load emails"));
  return res.json();
}

export async function markAllRead(mailboxId?: number | null): Promise<{ marked: number }> {
  const params = new URLSearchParams();
  if (mailboxId != null) params.set("mailbox_id", String(mailboxId));
  const qs = params.toString();
  const res = await apiFetch(`${API_BASE}/api/emails/mark-all-read${qs ? `?${qs}` : ""}`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(await readApiError(res, "Failed to mark messages read"));
  return res.json();
}

export async function fetchEmailConversation(id: number): Promise<EmailItem[]> {
  const res = await apiFetch(`${API_BASE}/api/emails/${id}/conversation`);
  if (!res.ok) throw new Error(await readApiError(res, "Failed to load conversation"));
  return res.json();
}

const DETAIL_CACHE_LIMIT = 30;
const detailCache = new Map<number, EmailDetail>();
const detailInflight = new Map<number, Promise<EmailDetail>>();

function rememberEmailDetail(detail: EmailDetail): void {
  detailCache.delete(detail.id);
  detailCache.set(detail.id, detail);
  if (detailCache.size <= DETAIL_CACHE_LIMIT) return;
  const oldest = detailCache.keys().next().value;
  if (oldest != null) detailCache.delete(oldest);
}

export function peekEmailDetail(id: number): EmailDetail | undefined {
  return detailCache.get(id);
}

export function peekEmailDetailRequest(id: number): Promise<EmailDetail> | undefined {
  return detailInflight.get(id);
}

/** Warm the body cache without marking the message read. */
export function prefetchEmailDetail(id: number): void {
  if (detailCache.has(id) || detailInflight.has(id)) return;
  void fetchEmailDetail(id, false).catch(() => {});
}

export async function fetchEmailDetail(id: number, markRead = true, markThread = false): Promise<EmailDetail> {
  if (!markRead) {
    const cached = detailCache.get(id);
    if (cached) return cached;
    const pending = detailInflight.get(id);
    if (pending) return pending;
  }
  const params = new URLSearchParams();
  if (!markRead) params.set("mark_read", "false");
  if (markRead && markThread) params.set("mark_thread", "true");
  const suffix = params.size ? `?${params.toString()}` : "";
  const request = (async () => {
    const res = await apiFetch(`${API_BASE}/api/emails/${id}${suffix}`, undefined, markRead ? 4 : 1);
    if (!res.ok) throw new Error(await readApiError(res, "Failed to load email"));
    const detail = (await res.json()) as EmailDetail;
    rememberEmailDetail(detail);
    return detail;
  })();
  if (!markRead) {
    detailInflight.set(id, request);
    void request.finally(() => {
      if (detailInflight.get(id) === request) detailInflight.delete(id);
    });
  }
  return request;
}

export async function updateEmailLabel(
  id: number,
  label: EmailLabel,
  saveTraining: boolean,
  interviewSubtype?: InterviewSubtype
): Promise<EmailDetail> {
  const res = await apiFetch(`${API_BASE}/api/emails/${id}/label`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ label, save_training: saveTraining, interview_subtype: interviewSubtype }),
  });
  if (!res.ok) throw new Error(await readApiError(res, "Failed to update category"));
  return res.json();
}

export interface ClassifyPromptStatus {
  unused_count: number;
  active_version_id: number | null;
  active_source: string | null;
  example_count: number;
  updated_at: string | null;
}

export interface ClassifyPromptUpdateResult {
  ok: boolean;
  example_count: number;
  prompt_version_id: number | null;
  message: string;
}

export async function fetchClassifyPromptStatus(): Promise<ClassifyPromptStatus> {
  const res = await apiFetch(`${API_BASE}/api/classify/prompt`);
  if (!res.ok) throw new Error(await readApiError(res, "Failed to load prompt status"));
  return res.json();
}

export async function updateClassifyPrompt(): Promise<ClassifyPromptUpdateResult> {
  const res = await apiFetch(`${API_BASE}/api/classify/prompt/update`, { method: "POST" });
  if (!res.ok) throw new Error(await readApiError(res, "Failed to update classify prompt"));
  return res.json();
}

export interface ClassifyTrainingExample {
  id: number;
  email_id: number;
  previous_label: string;
  corrected_label: string;
  previous_subtype: InterviewSubtype | null;
  corrected_subtype: InterviewSubtype | null;
  subject: string;
  sender: string;
  snippet: string;
  used_in_prompt_version_id: number | null;
  created_at: string | null;
}

export interface ClassifyTrainingPage {
  items: ClassifyTrainingExample[];
  unused_count: number;
  total: number;
}

export async function fetchClassifyTraining(opts?: {
  limit?: number;
  offset?: number;
}): Promise<ClassifyTrainingPage> {
  const params = new URLSearchParams();
  if (opts?.limit != null) params.set("limit", String(opts.limit));
  if (opts?.offset != null) params.set("offset", String(opts.offset));
  const qs = params.toString();
  const res = await apiFetch(`${API_BASE}/api/classify/training${qs ? `?${qs}` : ""}`);
  if (!res.ok) throw new Error(await readApiError(res, "Failed to load training data"));
  return res.json();
}

export interface ReclassifyStatus {
  mailbox_id: number;
  state: "running" | "done" | "stopped" | "error" | string;
  updated: number;
  processed: number;
  failed: number;
  total: number;
  message: string;
}

export async function fetchReclassifyStatus(id: number): Promise<ReclassifyStatus> {
  const res = await apiFetch(`${API_BASE}/api/mailboxes/${id}/reclassify-status`);
  if (!res.ok) throw new Error(await readApiError(res, "Failed to load reclassification status"));
  return res.json();
}

export interface EmailAttachment {
  name: string;
  content_type: string;
  content_base64: string;
}

export async function sendEmail(payload: {
  mailbox_id: number;
  to_address: string;
  subject: string;
  body_text: string;
  attachments?: EmailAttachment[];
  reply_to_email_id?: number;
}): Promise<{ ok: boolean; provider_message_id: string | null }> {
  const res = await apiFetch(`${API_BASE}/api/emails/send`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await readApiError(res, "Failed to send email"));
  return res.json();
}

export async function createAiReply(id: number, instruction = ""): Promise<{ body_text: string }> {
  const res = await apiFetch(`${API_BASE}/api/emails/${id}/ai-reply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ instruction }),
  });
  if (!res.ok) throw new Error(await readApiError(res, "Failed to create AI draft"));
  return res.json();
}

export interface AiSettings {
  provider: "openai" | "gemini" | "ollama";
  openai_model: string;
  openai_key_configured: boolean;
  openai_admin_key_configured: boolean;
  gemini_model: string;
  gemini_key_configured: boolean;
  ollama_url: string;
  ollama_model: string;
  billing_url: string;
}

export type CalendarEventKind = "availability" | "confirmed";
export type CalendarMeetingType = "video" | "phone" | "in_person" | "unspecified";
export type CalendarApplicationStatus = "active" | "passed" | "rejected" | "position_closed";
export type CalendarWorkModel = "remote" | "hybrid" | "on_site" | "unspecified";

export interface CalendarEvent {
  request_kind?: "invitation" | "reschedule" | null;
  id: number;
  mailbox_id: number;
  email_account: string;
  provider: Provider;
  source_email_id: number | null;
  kind: CalendarEventKind;
  origin: "email" | "manual";
  title: string;
  company: string;
  role: string;
  interviewer: string;
  work_model: CalendarWorkModel;
  job_url: string | null;
  description: string;
  start_at: string;
  end_at: string;
  source_timezone: string | null;
  interview_status: InterviewSubtype | null;
  application_status: CalendarApplicationStatus;
  meeting_type: CalendarMeetingType;
  meeting_provider: string | null;
  meeting_url: string | null;
  phone_number: string | null;
  phone_access_code: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface CalendarEventPayload {
  mailbox_id?: number;
  title?: string;
  company?: string;
  role?: string;
  interviewer?: string;
  work_model?: CalendarWorkModel;
  job_url?: string | null;
  description?: string;
  start_at?: string;
  end_at?: string;
  source_timezone?: string | null;
  interview_status?: InterviewSubtype;
  application_status?: CalendarApplicationStatus;
  meeting_type?: CalendarMeetingType;
  meeting_provider?: string | null;
  meeting_url?: string | null;
  phone_number?: string | null;
  phone_access_code?: string | null;
}

export async function fetchCalendarEvents(start: string, end: string, signal?: AbortSignal): Promise<CalendarEvent[]> {
  const params = new URLSearchParams({ start, end });
  const res = noteUnauthorized(await fetch(`${API_BASE}/api/calendar-events?${params}`, { signal, cache: "no-store", credentials: "include" }));
  if (!res.ok) throw new Error(await readApiError(res, "Failed to load calendar"));
  return res.json();
}

export interface PendingInterview {
  email_id: number;
  mailbox_id: number;
  email_account: string;
  kind: "invitation" | "reschedule";
  company: string;
  role: string;
  subject: string;
  received_at: string | null;
  slots: { event_id: number; start_at: string; end_at: string }[];
}

let pendingInterviewCache: PendingInterview[] | null = null;

export function peekPendingInterviews(): PendingInterview[] | null {
  return pendingInterviewCache;
}

export async function fetchPendingInterviews(signal?: AbortSignal): Promise<PendingInterview[]> {
  const res = noteUnauthorized(await fetch(`${API_BASE}/api/calendar-events/pending`, { signal, cache: "no-store", credentials: "include" }));
  if (!res.ok) throw new Error(await readApiError(res, "Failed to load pending interviews"));
  const rows = await res.json() as PendingInterview[];
  pendingInterviewCache = rows;
  return rows;
}

export async function setPendingInterviewDismissed(emailId: number, dismissed: boolean): Promise<void> {
  const res = noteUnauthorized(await fetch(`${API_BASE}/api/calendar-events/pending/${emailId}/dismiss`, {
    method: dismissed ? "POST" : "DELETE",
    credentials: "include",
  }));
  if (!res.ok) throw new Error(await readApiError(res, dismissed ? "Failed to discard interview" : "Failed to restore interview"));
}

export interface InterviewEmailAlert {
  email_id: number;
  mailbox_id: number;
  email_account: string;
  label: "interview_invitation" | "interview_scheduled";
  interview_subtype: string | null;
  company: string;
  role: string;
  subject: string;
  sender: string;
  received_at: string | null;
  event_id: number | null;
  start_at: string | null;
  end_at: string | null;
  meeting_type: string | null;
}

export async function fetchInterviewEmailAlerts(signal?: AbortSignal): Promise<InterviewEmailAlert[]> {
  const res = noteUnauthorized(await fetch(`${API_BASE}/api/emails/interview-alerts`, { signal, cache: "no-store", credentials: "include" }));
  if (!res.ok) throw new Error(await readApiError(res, "Failed to load interview alerts"));
  return res.json();
}

export async function fetchCalendarEventEmails(id: number): Promise<EmailDetail[]> {
  const res = await apiFetch(`${API_BASE}/api/calendar-events/${id}/emails`, undefined, 4);
  if (!res.ok) throw new Error(await readApiError(res, "Failed to load associated emails"));
  return res.json();
}

export async function createCalendarEvent(payload: CalendarEventPayload): Promise<CalendarEvent> {
  const res = await apiFetch(`${API_BASE}/api/calendar-events`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await readApiError(res, "Failed to create event"));
  return res.json();
}

export async function updateCalendarEvent(id: number, payload: CalendarEventPayload): Promise<CalendarEvent> {
  const res = await apiFetch(`${API_BASE}/api/calendar-events/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await readApiError(res, "Failed to update event"));
  return res.json();
}

export interface AiStatus {
  provider: "openai" | "gemini" | "ollama";
  model: string;
  ready: boolean;
  reason?: string | null;
}

export async function fetchAiStatus(): Promise<AiStatus> {
  const res = await apiFetch(`${API_BASE}/api/ai-settings/status`);
  if (!res.ok) throw new Error(await readApiError(res, "Failed to check AI status"));
  return res.json();
}

export async function fetchAiSettings(): Promise<AiSettings> {
  const res = await apiFetch(`${API_BASE}/api/ai-settings`);
  if (!res.ok) throw new Error(await readApiError(res, "Failed to load AI settings"));
  return res.json();
}

export async function fetchGeminiModels(): Promise<string[]> {
  const res = await apiFetch(`${API_BASE}/api/ai-settings/gemini-models`);
  if (!res.ok) throw new Error(await readApiError(res, "Failed to load Gemini models"));
  const data = await res.json();
  return Array.isArray(data.models) ? data.models : [];
}

export async function saveAiSettings(payload: {
  provider: "openai" | "gemini" | "ollama";
  openai_model: string;
  openai_api_key?: string;
  openai_admin_key?: string;
  gemini_model: string;
  gemini_api_key?: string;
  ollama_url: string;
  ollama_model: string;
}): Promise<AiSettings> {
  const res = await apiFetch(`${API_BASE}/api/ai-settings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await readApiError(res, "Failed to save AI settings"));
  return res.json();
}

export interface OpenAiCosts {
  month_spend_usd: number;
  currency: "usd";
  period_start: string;
  as_of: string;
  credit_balance_available: false;
}

export async function fetchOpenAiCosts(): Promise<OpenAiCosts> {
  const res = await apiFetch(`${API_BASE}/api/ai-settings/openai-costs`);
  if (!res.ok) throw new Error(await readApiError(res, "Failed to load OpenAI costs"));
  return res.json();
}

export async function fetchOllamaModels(url: string): Promise<string[]> {
  const res = await apiFetch(`${API_BASE}/api/ai-settings/ollama-models?url=${encodeURIComponent(url)}`);
  if (!res.ok) throw new Error(await readApiError(res, "Failed to load Ollama models"));
  return (await res.json()).models;
}

export async function deleteClassifyTrainingExample(id: number): Promise<{ deleted: number }> {
  const res = await apiFetch(`${API_BASE}/api/classify/training/${id}`, { method: "DELETE" });
  if (!res.ok) throw new Error(await readApiError(res, "Failed to delete training example"));
  return res.json();
}

export async function deleteUnusedClassifyTraining(): Promise<{ deleted: number }> {
  const res = await apiFetch(`${API_BASE}/api/classify/training/unused`, { method: "DELETE" });
  if (!res.ok) throw new Error(await readApiError(res, "Failed to delete unused training examples"));
  return res.json();
}

export interface MailboxLabelStats {
  mailbox_id: number | null;
  date_from: string;
  date_to: string;
  total: number;
  label_counts: Record<string, number>;
}

export interface LabelTimeline {
  mailbox_id: number;
  label: string;
  bucket: "hour" | "day";
  date_from: string;
  date_to: string;
  buckets: { bucket: string; count: number }[];
}

export interface OutcomeEntry {
  company: string;
  role: string;
  received_at: string | null;
  subject: string;
}

export interface MailboxOutcomes {
  mailbox_id: number;
  date_from: string;
  date_to: string;
  application_confirmation: OutcomeEntry[];
  rejected_closed: OutcomeEntry[];
  screening: OutcomeEntry[];
  interview_scheduled: OutcomeEntry[];
  items: OutcomeEntry[];
  total: number;
  label: string | null;
}

export type OutcomeLabel = "application_confirmation" | "rejected_closed" | "screening" | "interview_scheduled" | "interview_invitation";

function statsRangeParams(dateFrom: string, dateTo: string): URLSearchParams {
  return new URLSearchParams({
    date_from: dateFrom,
    date_to: dateTo,
  });
}

export async function fetchMailboxLabelStats(
  mailboxId: number | "all",
  dateFrom: string,
  dateTo: string
): Promise<MailboxLabelStats> {
  const params = statsRangeParams(dateFrom, dateTo);
  const path = mailboxId === "all" ? "/api/mailboxes/label-stats" : `/api/mailboxes/${mailboxId}/label-stats`;
  const res = await apiFetch(`${API_BASE}${path}?${params}`);
  if (!res.ok) throw new Error(await readApiError(res, "Failed to load category statistics"));
  return res.json();
}

export async function fetchMailboxLabelTimeline(
  mailboxId: number,
  label: EmailLabel,
  dateFrom: string,
  dateTo: string
): Promise<LabelTimeline> {
  const params = statsRangeParams(dateFrom, dateTo);
  params.set("label", label);
  const res = await apiFetch(`${API_BASE}/api/mailboxes/${mailboxId}/label-timeline?${params}`);
  if (!res.ok) throw new Error(await readApiError(res, "Failed to load category timeline"));
  return res.json();
}

export async function fetchMailboxOutcomes(
  mailboxId: number,
  dateFrom: string,
  dateTo: string,
  page?: { label: OutcomeLabel; limit: number; offset: number }
): Promise<MailboxOutcomes> {
  const params = statsRangeParams(dateFrom, dateTo);
  if (page) {
    params.set("label", page.label);
    params.set("limit", String(page.limit));
    params.set("offset", String(page.offset));
  }
  const res = await apiFetch(`${API_BASE}/api/mailboxes/${mailboxId}/outcomes?${params}`);
  if (!res.ok) throw new Error(await readApiError(res, "Failed to load company and role lists"));
  return res.json();
}

export async function disconnectMailbox(id: number): Promise<void> {
  const res = await apiFetch(`${API_BASE}/api/mailboxes/${id}`, { method: "DELETE" });
  if (!res.ok) throw new Error("Failed to disconnect mailbox");
}

export interface SyncStartResult {
  ok: boolean;
  state?: string;
  listed?: number;
  skipped?: number;
  imported?: number;
  failed?: number;
  total?: number;
  message?: string;
}

export async function syncMailbox(id: number): Promise<SyncStartResult> {
  const res = await apiFetch(`${API_BASE}/api/mailboxes/${id}/sync`, { method: "POST" });
  if (!res.ok) throw new Error(await readApiError(res, "Failed to sync mailbox"));
  return res.json();
}

export async function extractMailboxOutcomes(mailboxId: number, label: "rejected_closed" | "interview_invitation"): Promise<{ ok: boolean; message: string }> {
  const params = new URLSearchParams({ label });
  const res = await apiFetch(`${API_BASE}/api/mailboxes/${mailboxId}/outcomes/extract?${params}`, { method: "POST" });
  if (!res.ok) throw new Error(await readApiError(res, "Failed to start AI extraction"));
  return res.json();
}

export async function fullRescanMailbox(id: number): Promise<SyncStartResult> {
  const res = await apiFetch(`${API_BASE}/api/mailboxes/${id}/sync/full`, { method: "POST" });
  if (!res.ok) throw new Error(await readApiError(res, "Failed to rescan mailbox"));
  return res.json();
}

export async function reclassifyMailbox(id: number): Promise<SyncStartResult> {
  const res = await apiFetch(`${API_BASE}/api/mailboxes/${id}/reclassify`, { method: "POST" });
  if (!res.ok) throw new Error(await readApiError(res, "Failed to reclassify mailbox"));
  return res.json();
}

export async function stopSyncMailbox(id: number): Promise<SyncStartResult> {
  const res = await apiFetch(`${API_BASE}/api/mailboxes/${id}/sync/stop`, { method: "POST" });
  if (!res.ok) throw new Error(await readApiError(res, "Failed to stop sync"));
  return res.json();
}

export async function stopReclassifyMailbox(id: number): Promise<SyncStartResult> {
  const res = await apiFetch(`${API_BASE}/api/mailboxes/${id}/reclassify/stop`, { method: "POST" });
  if (!res.ok) throw new Error(await readApiError(res, "Failed to stop reclassify"));
  return res.json();
}

export function oauthStartUrl(
  provider: Provider,
  opts: { email: string; name?: string }
): string {
  const qs = new URLSearchParams({
    email: opts.email.trim(),
    return_to: window.location.origin,
  });
  if (opts.name?.trim()) qs.set("name", opts.name.trim());
  return `${API_BASE}/api/auth/${provider}/start?${qs.toString()}`;
}

export function eventsUrl(): string {
  return `${API_BASE}/api/events`;
}

export interface EventStream {
  addEventListener(name: string, listener: EventListener): void;
  removeEventListener(name: string, listener: EventListener): void;
  close(): void;
}

let sharedEvents: EventSource | null = null;
let sharedEventUsers = 0;

/** Every open EventSource pins one of the browser's six HTTP/1.1 connections, so components share one. */
export function openEvents(): EventStream {
  if (!sharedEvents) sharedEvents = new EventSource(eventsUrl());
  const source = sharedEvents;
  sharedEventUsers += 1;
  const added: Array<[string, EventListener]> = [];
  let closed = false;
  return {
    addEventListener(name, listener) {
      source.addEventListener(name, listener);
      added.push([name, listener]);
    },
    removeEventListener(name, listener) {
      source.removeEventListener(name, listener);
    },
    close() {
      if (closed) return;
      closed = true;
      added.forEach(([name, listener]) => source.removeEventListener(name, listener));
      sharedEventUsers -= 1;
      // Deferred so a remount in the same tick reuses the open stream.
      window.setTimeout(() => {
        if (sharedEventUsers === 0 && sharedEvents === source) {
          source.close();
          sharedEvents = null;
        }
      }, 0);
    },
  };
}

export interface SessionUser {
  id: number;
  email: string;
  display_name: string | null;
  is_admin: boolean;
}

export interface SignInAccount extends SessionUser {
  mailbox_count: number;
  created_at: string | null;
  last_login_at: string | null;
}

/** Returns the signed-in account, or null when there is no valid session. */
export async function fetchSession(): Promise<SessionUser | null> {
  const res = await apiFetch(`${API_BASE}/api/session/me`);
  if (res.status === 401) return null;
  if (!res.ok) throw new Error(await readApiError(res, "Could not check sign-in"));
  return res.json();
}

export async function signIn(email: string, password: string): Promise<SessionUser> {
  const res = await apiFetch(`${API_BASE}/api/session/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  }, 1);
  if (!res.ok) throw new Error(await readApiError(res, "Sign-in failed"));
  return res.json();
}

export async function signOut(): Promise<void> {
  await apiFetch(`${API_BASE}/api/session/logout`, { method: "POST" }, 1);
}

export async function changeOwnPassword(currentPassword: string, newPassword: string): Promise<void> {
  const res = await apiFetch(`${API_BASE}/api/session/password`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  }, 1);
  if (!res.ok) throw new Error(await readApiError(res, "Could not change password"));
}

export async function fetchSignInAccounts(): Promise<SignInAccount[]> {
  const res = await apiFetch(`${API_BASE}/api/users`);
  if (!res.ok) throw new Error(await readApiError(res, "Could not load sign-in accounts"));
  return res.json();
}

export async function createSignInAccount(payload: { email: string; password: string; display_name?: string; is_admin?: boolean }): Promise<SignInAccount> {
  const res = await apiFetch(`${API_BASE}/api/users`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }, 1);
  if (!res.ok) throw new Error(await readApiError(res, "Could not add sign-in account"));
  return res.json();
}

export async function updateSignInAccount(id: number, payload: { password?: string; display_name?: string; is_admin?: boolean }): Promise<SignInAccount> {
  const res = await apiFetch(`${API_BASE}/api/users/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }, 1);
  if (!res.ok) throw new Error(await readApiError(res, "Could not update sign-in account"));
  return res.json();
}

export async function deleteSignInAccount(id: number): Promise<{ removed_mailboxes: number }> {
  const res = await apiFetch(`${API_BASE}/api/users/${id}`, { method: "DELETE" }, 1);
  if (!res.ok) throw new Error(await readApiError(res, "Could not delete sign-in account"));
  return res.json();
}
