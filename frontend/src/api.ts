export type EmailLabel =
  | "application_confirmation"
  | "application_action_required"
  | "screening"
  | "assessment"
  | "interview_invitation"
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
  is_active: boolean;
  created_at: string | null;
}

export type AutoSyncState = "active" | "syncing" | "no_account" | "stopped" | "error";

export interface AutoSyncStatus {
  state: AutoSyncState;
  interval_seconds: number;
  connected_accounts: number;
  webhook_accounts: number;
  last_check_at: string | null;
  last_error: string | null;
  problem_mailboxes: { mailbox_id: number; email_address: string; message: string }[];
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

async function apiFetch(url: string, init?: RequestInit, attempts = 3): Promise<Response> {
  let lastError: unknown = null;
  for (let i = 0; i < attempts; i += 1) {
    try {
      const res = await fetch(url, init);
      return res;
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

async function autoSyncRequest(method: "GET" | "POST", path: string): Promise<AutoSyncStatus> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 5000);
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      method,
      signal: controller.signal,
      cache: "no-store",
    });
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
}

export async function fetchEmails(opts?: {
  label?: string;
  interviewSubtype?: InterviewSubtype | null;
  folder?: string;
  mailboxId?: number | null;
  query?: string | null;
  inboxType?: "default" | "unread_first";
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

export async function fetchEmailDetail(id: number): Promise<EmailDetail> {
  const res = await apiFetch(`${API_BASE}/api/emails/${id}`, undefined, 4);
  if (!res.ok) throw new Error(await readApiError(res, "Failed to load email"));
  return res.json();
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
}): Promise<{ ok: boolean; provider_message_id: string | null }> {
  const res = await apiFetch(`${API_BASE}/api/emails/send`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await readApiError(res, "Failed to send email"));
  return res.json();
}

export async function createAiReply(id: number): Promise<{ body_text: string }> {
  const res = await apiFetch(`${API_BASE}/api/emails/${id}/ai-reply`, { method: "POST" });
  if (!res.ok) throw new Error(await readApiError(res, "Failed to create AI draft"));
  return res.json();
}

export interface AiSettings {
  provider: "openai" | "ollama";
  openai_model: string;
  openai_key_configured: boolean;
  openai_admin_key_configured: boolean;
  ollama_url: string;
  ollama_model: string;
  billing_url: string;
}

export async function fetchAiSettings(): Promise<AiSettings> {
  const res = await apiFetch(`${API_BASE}/api/ai-settings`);
  if (!res.ok) throw new Error(await readApiError(res, "Failed to load AI settings"));
  return res.json();
}

export async function saveAiSettings(payload: {
  provider: "openai" | "ollama";
  openai_model: string;
  openai_api_key?: string;
  openai_admin_key?: string;
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
  });
  if (opts.name?.trim()) qs.set("name", opts.name.trim());
  return `${API_BASE}/api/auth/${provider}/start?${qs.toString()}`;
}

export function eventsUrl(): string {
  return `${API_BASE}/api/events`;
}
