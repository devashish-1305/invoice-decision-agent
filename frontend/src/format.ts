/** Indian number format: 165000 -> "₹1,65,000.00" (or "₹1,65,000" without paise). Money arrives as strings. */
export function inr(value: string | number | null | undefined, paise = true): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (Number.isNaN(n)) return "—";
  const [whole, frac] = Math.abs(n).toFixed(2).split(".");
  const grouped =
    whole.length > 3
      ? whole.slice(0, -3).replace(/\B(?=(\d{2})+(?!\d))/g, ",") + "," + whole.slice(-3)
      : whole;
  const sign = n < 0 ? "-" : "";
  return `${sign}₹${grouped}${paise ? "." + frac : ""}`;
}

export function ms(value: number | null | undefined): string {
  if (value === null || value === undefined) return "";
  return value < 1000 ? `${value} ms` : `${(value / 1000).toFixed(1)} s`;
}

export function when(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
}

export function bytes(n: number | null | undefined): string {
  if (!n) return "—";
  return n < 1024 * 1024 ? `${(n / 1024).toFixed(0)} KB` : `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/** "108.0" -> "108", "75.0" -> "75", "101.3" -> "101.3" */
export function pct(value: string | number): string {
  return String(Number(value));
}
