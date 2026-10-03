export function defaultAccountColor(mailboxId: number): string {
  const hue = (Math.abs(mailboxId) * 47) % 360;
  return hslToHex(hue, 68, 44);
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

function hslToHex(hue: number, saturation: number, lightness: number): string {
  const sat = saturation / 100;
  const light = lightness / 100;
  const chroma = sat * Math.min(light, 1 - light);
  const channel = (offset: number) => {
    const k = (offset + hue / 30) % 12;
    const amount = light - chroma * Math.max(Math.min(k - 3, 9 - k, 1), -1);
    return Math.round(255 * amount).toString(16).padStart(2, "0");
  };
  return `#${channel(0)}${channel(8)}${channel(4)}`;
}
