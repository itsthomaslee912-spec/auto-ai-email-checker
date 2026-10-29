export type ApplicationCopyAction = {
  id: string;
  kind: "code" | "link";
  label: string;
  value: string;
};

const CODE_STOPWORDS = new Set(
  [
    "code",
    "field",
    "into",
    "paste",
    "copy",
    "this",
    "that",
    "your",
    "application",
    "security",
    "verification",
    "login",
    "access",
    "passcode",
    "password",
    "confirm",
    "confirmation",
    "account",
    "email",
    "please",
    "enter",
    "below",
    "above",
    "here",
    "link",
    "click",
    "submit",
    "resubmit",
    "after",
    "before",
    "greenhouse",
    "workday",
    "recruiting",
    "street",
    "avenue",
    "floor",
    "york",
    "token",
    "message",
    "recruit",
  ].map((word) => word.toLowerCase()),
);

function stripHtml(value: string): string {
  return value
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/(p|div|tr|li|h\d|td|th)>/gi, "\n")
    .replace(/<[^>]+>/g, " ")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{2,}/g, "\n")
    .replace(/[ \t]{2,}/g, " ")
    .trim();
}

/** Body only — subject often names the employer ("… to Chainguard") and must not be parsed as a code. */
function codeSearchBlob(parts: {
  html?: string | null;
  text?: string | null;
  snippet?: string | null;
}): string {
  return [parts.snippet ?? "", parts.text ?? "", parts.html ? stripHtml(parts.html) : ""]
    .filter(Boolean)
    .join("\n");
}

function fullBlob(parts: {
  subject?: string | null;
  html?: string | null;
  text?: string | null;
  snippet?: string | null;
}): string {
  return [parts.subject ?? "", ...codeSearchBlob(parts).split("\n")].filter(Boolean).join("\n");
}

function uppercaseCount(value: string): number {
  return (value.match(/[A-Z]/g) || []).length;
}

function looksLikeCode(value: string): boolean {
  const trimmed = value.trim();
  if (trimmed.length < 4 || trimmed.length > 12) return false;
  const lower = trimmed.toLowerCase();
  if (CODE_STOPWORDS.has(lower)) return false;
  if (!/^[A-Za-z0-9-]+$/.test(trimmed)) return false;

  if (/^\d{4,8}$/.test(trimmed)) return true;
  if (/[A-Za-z]/.test(trimmed) && /\d/.test(trimmed)) return true;

  // Letter-only tokens must not be a single Title Case word (Chainguard, Street).
  if (/^[A-Za-z]+$/.test(trimmed)) {
    if (/^[A-Z][a-z]+$/.test(trimmed)) return false;
    if (uppercaseCount(trimmed) < 2) return false;
    if (!/[a-z]/.test(trimmed) || !/[A-Z]/.test(trimmed)) return false;
    return trimmed.length >= 6;
  }

  return false;
}

type CodeCandidate = { value: string; score: number };

function addCandidate(list: CodeCandidate[], raw: string, score: number, seen: Set<string>) {
  const value = raw.trim();
  if (!looksLikeCode(value) || seen.has(value)) return;
  seen.add(value);
  list.push({ value, score });
}

function pickBestCode(candidates: CodeCandidate[]): ApplicationCopyAction | null {
  if (!candidates.length) return null;
  candidates.sort((a, b) => b.score - a.score || b.value.length - a.value.length);
  const value = candidates[0].value;
  return {
    id: `code-${value}`,
    kind: "code",
    label: "Verification code",
    value,
  };
}

function extractCode(body: string): ApplicationCopyAction | null {
  if (!body.trim()) return null;

  const candidates: CodeCandidate[] = [];
  const seen = new Set<string>();

  // A bare one-word line is not enough evidence that the word is a code.
  // Email signatures and templates routinely put brand names such as
  // "BlackBerry" on their own line, and camel-cased names otherwise look like
  // letter-only access codes. Only consider a standalone token when the two
  // preceding lines explicitly identify it as a code.
  const lines = body.split("\n");
  for (let index = 0; index < lines.length; index += 1) {
    const token = lines[index].trim();
    if (!token || /\s/.test(token)) continue;
    const context = lines.slice(Math.max(0, index - 2), index).join(" ");
    if (/\b(?:verification|security|login|access|one[- ]time|otp|authentication)?\s*(?:code|pin|passcode|token)\b/i.test(context)) {
      addCandidate(candidates, token, 80, seen);
    }
  }

  const labeled = new RegExp(
    String.raw`\b(?:verification|security|login|access|one[- ]time|otp|authentication)\s+(?:code|pin|passcode)\s*(?:is|:)\s*["']?([A-Za-z0-9-]{4,12})\b`,
    "gi",
  );
  for (const match of body.matchAll(labeled)) addCandidate(candidates, match[1] || "", 95, seen);

  const codeIs = /\b(?:code|pin|passcode)\s*(?:is|:)\s*["']?([A-Za-z0-9-]{4,12})\b/gi;
  for (const match of body.matchAll(codeIs)) addCandidate(candidates, match[1] || "", 90, seen);

  const afterInstruction =
    /(?:copy and paste this code into the security code field|security code field on your application)[^\n]{0,40}(?:\n+\s*|\s+)["']?([A-Za-z0-9-]{6,12})\b/gi;
  for (const match of body.matchAll(afterInstruction)) addCandidate(candidates, match[1] || "", 88, seen);

  const afterInstructionMultiline =
    /(?:security|verification)\s+code[^\n]{0,80}\n+\s*["']?([A-Za-z0-9-]{6,12})\s*(?:\n|$)/gi;
  for (const match of body.matchAll(afterInstructionMultiline)) addCandidate(candidates, match[1] || "", 86, seen);

  return pickBestCode(candidates);
}

const LINK_HOST_HINTS = [
  "greenhouse.io",
  "myworkday",
  "workday.com",
  "wd1.myworkday",
  "wd3.myworkday",
  "wd5.myworkday",
  "smartrecruiters",
  "icims.com",
  "lever.co",
  "ashbyhq.com",
  "jobvite.com",
  "eightfold.ai",
  "taleo.net",
  "successfactors",
];

const LINK_PATH_HINTS = [
  "verify",
  "verification",
  "validate",
  "activate",
  "activation",
  "confirm",
  "password",
  "reset",
  "authentication",
  "signin",
  "login",
  "token",
  "magic",
  "candidate",
];

function linkLabel(url: string): string {
  const lower = url.toLowerCase();
  if (/(verify|verification|validate|activate|activation|confirm|token|magic|auth)/.test(lower)) return "Verification link";
  if (/(password|reset)/.test(lower)) return "Password reset link";
  if (/(greenhouse|workday|myworkday|smartrecruiters|icims|lever|ashby)/.test(lower)) return "Application link";
  return "Action link";
}

function linkLooksActionable(url: string): boolean {
  try {
    const parsed = new URL(url);
    const host = parsed.hostname.toLowerCase();
    const path = `${parsed.pathname}${parsed.search}`.toLowerCase();
    if (LINK_HOST_HINTS.some((hint) => host.includes(hint))) return true;
    if (LINK_PATH_HINTS.some((hint) => path.includes(hint))) return true;
    return /(?:verify|verification|confirm|reset|password|token|auth)/.test(url.toLowerCase());
  } catch {
    return false;
  }
}

function cleanUrl(raw: string): string {
  return raw.replace(/[),.;]+$/g, "");
}

function extractBestLink(blob: string): ApplicationCopyAction | null {
  const linkRe = /https?:\/\/[^\s<>"')\]]+/gi;
  for (const match of blob.matchAll(linkRe)) {
    const value = cleanUrl(match[0]);
    if (!value || !linkLooksActionable(value)) continue;
    return {
      id: `link-${value}`,
      kind: "link",
      label: linkLabel(value),
      value,
    };
  }
  return null;
}

/** At most one copy action: the best verification code, or else one actionable link. */
export function extractApplicationCopyActions(parts: {
  subject?: string | null;
  html?: string | null;
  text?: string | null;
  snippet?: string | null;
}): ApplicationCopyAction[] {
  const body = codeSearchBlob(parts);
  const code = extractCode(body);
  if (code) return [code];

  const link = extractBestLink(fullBlob(parts));
  return link ? [link] : [];
}
