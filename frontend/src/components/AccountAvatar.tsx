export const ACCOUNT_AVATAR_OPTIONS = [
  { key: "person", name: "Simple" },
  { key: "cartoon-smile", name: "Friendly" },
  { key: "cartoon-glasses", name: "Glasses" },
  { key: "cartoon-headphones", name: "Headset" },
  { key: "cartoon-cap", name: "Cap" },
] as const;

export type AccountAvatarKey = typeof ACCOUNT_AVATAR_OPTIONS[number]["key"];

const AVAILABLE_KEYS = new Set<string>(ACCOUNT_AVATAR_OPTIONS.map((option) => option.key));
const LEGACY_KEYS: Record<string, AccountAvatarKey> = {
  initials: "person",
  "engineer-1": "cartoon-smile",
  "engineer-2": "cartoon-glasses",
  "engineer-3": "cartoon-headphones",
  "engineer-4": "cartoon-cap",
  "engineer-5": "cartoon-smile",
  "engineer-6": "cartoon-glasses",
  "engineer-7": "cartoon-headphones",
  "engineer-8": "cartoon-cap",
};

export function accountAvatarKey(value: string | null | undefined): AccountAvatarKey {
  const key = (value || "").trim().toLowerCase();
  if (AVAILABLE_KEYS.has(key)) return key as AccountAvatarKey;
  return LEGACY_KEYS[key] ?? "person";
}

export function AccountAvatarGlyph({ avatarKey }: { avatarKey?: string | null; initials: string }) {
  const key = accountAvatarKey(avatarKey);
  if (key === "person") {
    return (
      <svg className="account-avatar-art" viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="8" r="3.25" />
        <path d="M5.75 19c.55-3.45 2.62-5.25 6.25-5.25s5.7 1.8 6.25 5.25" />
      </svg>
    );
  }
  return (
    <svg className={`account-avatar-art is-${key}`} viewBox="0 0 24 24" aria-hidden="true">
      <circle className="avatar-face" cx="12" cy="12" r="7.25" />
      {key === "cartoon-smile" && <>
        <path d="M6.2 9.1c1.1-3 3.35-4.45 6.6-4.25 2.4.15 4.1 1.25 5.05 3.3-2.45.45-4.65-.15-6.6-1.8-1.1 1.45-2.8 2.35-5.05 2.75Z" />
        <circle className="avatar-dot" cx="9.3" cy="11.6" r=".65" /><circle className="avatar-dot" cx="14.7" cy="11.6" r=".65" />
        <path d="M9.15 15c1.75 1.45 3.95 1.45 5.7 0" />
      </>}
      {key === "cartoon-glasses" && <>
        <path d="M6.4 8.5c1.35-2.6 3.55-3.85 6.55-3.6 2.2.18 3.78 1.12 4.75 2.8" />
        <circle cx="9.2" cy="11.7" r="1.75" /><circle cx="14.8" cy="11.7" r="1.75" /><path d="M10.95 11.7h2.1M6.2 11l1.25.35m9.1 0L17.8 11" />
        <path d="M10 15.2c1.25.7 2.75.7 4 0" />
      </>}
      {key === "cartoon-headphones" && <>
        <path d="M6.4 11v-1c0-3.1 2.3-5.25 5.6-5.25s5.6 2.15 5.6 5.25v1M6.4 10.8H5.25v4.1H7m10-4.1h1.15v4.1H16.4" />
        <circle className="avatar-dot" cx="9.6" cy="11.6" r=".6" /><circle className="avatar-dot" cx="14.4" cy="11.6" r=".6" />
        <path d="M9.6 15c1.45 1.05 3.35 1.05 4.8 0" />
      </>}
      {key === "cartoon-cap" && <>
        <path d="M6.2 9.2c.35-3 2.5-4.7 5.8-4.7s5.35 1.55 5.8 4.35c-3.85-1.3-7.7-1.18-11.6.35Z" /><path d="M12.4 8.15c2.45-.25 4.35.1 5.7 1.05" />
        <circle className="avatar-dot" cx="9.5" cy="12" r=".6" /><circle className="avatar-dot" cx="14.5" cy="12" r=".6" />
        <path d="M9.5 15.2c1.55 1.05 3.45 1.05 5 0" />
      </>}
    </svg>
  );
}
