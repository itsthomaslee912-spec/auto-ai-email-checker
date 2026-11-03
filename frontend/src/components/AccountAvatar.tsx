export const ACCOUNT_AVATAR_OPTIONS = [
  { key: "person", name: "Male character 1" },
  { key: "cartoon-smile", name: "Male character 2" },
  { key: "cartoon-glasses", name: "Male character 3" },
  { key: "cartoon-headphones", name: "Male character 4" },
  { key: "cartoon-cap", name: "Male character 5" },
  { key: "cartoon-bun", name: "Male character 6" },
  { key: "cartoon-beanie", name: "Male character 7" },
  { key: "cartoon-curls", name: "Male character 8" },
  { key: "cartoon-hijab", name: "Male character 9" },
  { key: "cartoon-hoodie", name: "Male character 10" },
] as const;

export type AccountAvatarKey = typeof ACCOUNT_AVATAR_OPTIONS[number]["key"];

const AVAILABLE_KEYS = new Set<string>(ACCOUNT_AVATAR_OPTIONS.map((option) => option.key));
const LEGACY_KEYS: Record<string, AccountAvatarKey> = {
  initials: "person",
  "engineer-1": "cartoon-smile",
  "engineer-2": "cartoon-glasses",
  "engineer-3": "cartoon-headphones",
  "engineer-4": "cartoon-cap",
  "engineer-5": "cartoon-bun",
  "engineer-6": "cartoon-beanie",
  "engineer-7": "cartoon-curls",
  "engineer-8": "cartoon-hoodie",
};

export function accountAvatarKey(value: string | null | undefined): AccountAvatarKey {
  const key = (value || "").trim().toLowerCase();
  if (AVAILABLE_KEYS.has(key)) return key as AccountAvatarKey;
  return LEGACY_KEYS[key] ?? "person";
}

function Face({ eyes = true }: { eyes?: boolean }) {
  return <>
    {eyes ? <><circle className="avatar-eye" cx="12.55" cy="14.65" r=".7" /><circle className="avatar-eye" cx="19.45" cy="14.65" r=".7" /></> : null}
    <path className="avatar-smile" d="M12.8 18.2c1.95 1.45 4.45 1.45 6.4 0" />
  </>;
}

export function AccountAvatarGlyph({ avatarKey }: { avatarKey?: string | null; initials: string }) {
  const key = accountAvatarKey(avatarKey);
  return (
    <svg className={`account-avatar-art is-cartoon-character is-${key}`} viewBox="0 0 32 32" aria-hidden="true">
      <path className="avatar-shoulders" d="M5 30c.7-5.4 4.25-8.15 11-8.15S26.3 24.6 27 30" />
      <circle className="avatar-ear" cx="8.15" cy="15.15" r="2.05" />
      <circle className="avatar-ear" cx="23.85" cy="15.15" r="2.05" />
      <path className="avatar-neck" d="M12.4 20.1v3.15h7.2V20.1" />
      <ellipse className="avatar-face" cx="16" cy="14.2" rx="7.4" ry="8.2" />

      {key === "person" && <>
        <path className="avatar-hair" d="M8.8 11.65c.2-4.8 2.75-7.25 7.35-7.25 4.45 0 6.95 2.4 7.1 7.15-2.8-.25-5.25-1.45-7.3-3.6-1.75 1.85-4.15 3.1-7.15 3.7Z" />
        <Face />
      </>}

      {key === "cartoon-smile" && <>
        <path className="avatar-hair" d="M8.65 12.05c-.1-5 2.65-7.75 7.55-7.75 4.5 0 7.1 2.45 7.15 7.15-3.55.15-6.6-1.1-9.15-3.75-1.1 2.25-2.95 3.7-5.55 4.35Z" />
        <path className="avatar-brow" d="M11.2 13.1c.85-.55 1.7-.55 2.55 0m4.5 0c.85-.55 1.7-.55 2.55 0" />
        <Face />
      </>}

      {key === "cartoon-glasses" && <>
        <path className="avatar-hair" d="M8.8 11.5c.35-4.7 2.85-7.05 7.4-7.05 4.3 0 6.7 2.2 7 6.6-3.05-.2-5.7-1.3-7.95-3.3-1.65 1.65-3.8 2.9-6.45 3.75Z" />
        <circle className="avatar-glasses" cx="12.45" cy="14.7" r="2.35" /><circle className="avatar-glasses" cx="19.55" cy="14.7" r="2.35" />
        <path className="avatar-glasses" d="M14.8 14.7h2.4M8.7 14.15l1.4.25m11.8 0 1.4-.25" />
        <Face />
      </>}

      {key === "cartoon-headphones" && <>
        <path className="avatar-hair" d="M8.7 11.45C8.95 6.75 11.5 4.3 16 4.3s7.05 2.45 7.3 7.15c-2.75-.25-5.3-1.45-7.65-3.6-1.75 1.85-4.05 3.05-6.95 3.6Z" />
        <path className="avatar-headset" d="M7.15 15v-2.1C7.15 7.55 10.45 4 16 4s8.85 3.55 8.85 8.9V15M7.35 14.25H5.9v5.15h2.65m15.9-5.15h1.45v5.15h-2.65m1.2-.35c-.1 2.35-1.55 3.7-4.35 4" />
        <Face />
      </>}

      {key === "cartoon-cap" && <>
        <path className="avatar-hair" d="M9.05 10.45c.75-3.85 3.15-5.8 7.15-5.8 4.1 0 6.5 2.05 7.1 6.15-2.9-.4-5.45-1.55-7.65-3.45-1.6 1.6-3.8 2.65-6.6 3.1Z" />
        <path className="avatar-cap" d="M8.4 9.55c.55-4 3.1-6.15 7.6-6.15 4.35 0 6.85 1.95 7.55 5.6-5.15-1.25-10.2-1.05-15.15.55Z" /><path className="avatar-cap-brim" d="M16.6 8.05c3.45-.35 6.15.15 8.1 1.5" />
        <Face />
      </>}

      {key === "cartoon-bun" && <>
        <circle className="avatar-hair" cx="20.85" cy="5.05" r="3.25" />
        <path className="avatar-hair" d="M8.65 12c-.25-5.05 2.45-7.75 7.5-7.75 4.65 0 7.2 2.5 7.25 7.25-3.35-.1-6.2-1.4-8.55-3.9-1.25 2.3-3.3 3.75-6.2 4.4Z" />
        <path className="avatar-beard" d="M9.6 17.2c1.1 4.3 3.2 6.35 6.4 6.35s5.3-2.05 6.4-6.35c-1.7 1.75-3.85 2.6-6.4 2.6s-4.7-.85-6.4-2.6Z" />
        <Face />
      </>}

      {key === "cartoon-beanie" && <>
        <path className="avatar-hair" d="M9 11.5c.35-4.75 2.75-7.05 7.25-7.05 4.25 0 6.6 2.2 7.05 6.6-2.85-.2-5.4-1.35-7.65-3.45-1.65 1.75-3.85 3.05-6.65 3.9Z" />
        <path className="avatar-beanie" d="M8.15 9.25C8.9 4.8 11.55 2.55 16 2.55s7.1 2.25 7.85 6.7Z" /><path className="avatar-beanie-rim" d="M8.15 9.25h15.7v2.1H8.15Z" />
        <Face />
      </>}

      {key === "cartoon-curls" && <>
        <path className="avatar-curls" d="M8.2 14.2c-2.1-1.25-1.75-4.4.45-5.2-1-2.25 1.5-4.5 3.6-3.45.65-2.55 4.05-3.1 5.45-.95 2.1-1.45 4.8.35 4.45 2.8 2.55.2 3.55 3.25 1.7 4.9.95 1.9-.55 4.1-2.6 4.05l.25-6.05c-3.45.2-6.25-1.15-8.4-4.05-.65 2.55-2.25 4.35-4.8 5.4Z" />
        <Face />
      </>}

      {key === "cartoon-hijab" && <>
        <path className="avatar-hair" d="M8.55 11.5C8.9 6.7 11.4 4.2 16 4.2c4.55 0 7.1 2.45 7.4 7.1-3.55.05-6.35-1.25-8.4-3.9-1.15 2.15-3.3 3.5-6.45 4.1Z" />
        <path className="avatar-fade" d="M8.8 8.9c1.15-3.15 3.5-4.7 7.05-4.7 3.75 0 6.2 1.55 7.35 4.65-4.85-1.3-9.65-1.3-14.4.05Z" />
        <path className="avatar-mustache" d="M16 17.25c-1.15-1.15-2.35-1.1-3.6.15 1.25 1.3 2.45 1.3 3.6 0 1.15 1.3 2.35 1.3 3.6 0-1.25-1.25-2.45-1.3-3.6-.15Z" />
        <Face />
      </>}

      {key === "cartoon-hoodie" && <>
        <path className="avatar-hood" d="M6.8 29.8c.35-5.1 2.15-8.05 5.4-8.85-2.4-1.55-3.65-4.2-3.65-7.9C8.55 7.4 11.25 4.2 16 4.2s7.45 3.2 7.45 8.85c0 3.7-1.25 6.35-3.65 7.9 3.25.8 5.05 3.75 5.4 8.85Z" />
        <path className="avatar-hair" d="M9.45 11.45C9.8 7.1 12 5 16 5c3.9 0 6.1 2.05 6.5 6.15-2.65-.25-4.9-1.35-6.8-3.3-1.5 1.65-3.6 2.85-6.25 3.6Z" />
        <Face />
      </>}
    </svg>
  );
}
