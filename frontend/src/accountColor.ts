export const ACCOUNT_COLOR_PALETTE = [
  { name: "Blue", value: "#4f7df3" },
  { name: "Teal", value: "#14a99a" },
  { name: "Purple", value: "#8b5cf6" },
  { name: "Coral", value: "#ef6a5b" },
  { name: "Sky", value: "#169bd5" },
  { name: "Rose", value: "#df5f8f" },
  { name: "Indigo", value: "#6366d9" },
  { name: "Green", value: "#2f9e6f" },
  { name: "Amber", value: "#d4942d" },
  { name: "Slate", value: "#718096" },
] as const;

export function defaultAccountColor(mailboxId: number): string {
  const index = Math.abs(Math.trunc(mailboxId) - 1) % ACCOUNT_COLOR_PALETTE.length;
  return ACCOUNT_COLOR_PALETTE[index].value;
}

export function accountColor(mailbox: { id: number; calendar_color?: string | null }): string {
  const custom = mailbox.calendar_color?.trim().toLowerCase();
  if (custom && /^#[0-9a-f]{6}$/.test(custom)) return custom;
  return defaultAccountColor(mailbox.id);
}

export function accountInk(color: string): string {
  const match = /^#([0-9a-f]{6})$/i.exec(color);
  if (!match) return "#f8fafc";
  const value = Number.parseInt(match[1], 16);
  const red = (value >> 16) & 255;
  const green = (value >> 8) & 255;
  const blue = value & 255;
  const luminance = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255;
  return luminance > 0.62 ? "#0f172a" : "#f8fafc";
}
