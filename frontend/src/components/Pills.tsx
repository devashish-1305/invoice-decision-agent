/** Status pills and badges, styled exactly from the design tokens. */
import type { ReactNode } from "react";
import type { Decision, Risk } from "../api";

const PILL = "inline-flex items-center whitespace-nowrap rounded-full px-2 py-[3px] text-[11px] font-semibold leading-none";

export function Pill({ tone, children }: { tone: "ok" | "warn" | "bad" | "solid-bad" | "accent" | "neutral"; children: ReactNode }) {
  const tones = {
    ok: "bg-ok-bg text-ok", warn: "bg-warn-bg text-warn", bad: "bg-bad-bg text-bad",
    "solid-bad": "bg-bad text-white", accent: "bg-accent-bg text-accent-text", neutral: "bg-divider text-muted",
  };
  return <span className={`${PILL} ${tones[tone]}`}>{children}</span>;
}

export function ResultPill({ result }: { result: "PASS" | "WARN" | "FAIL" }) {
  return <Pill tone={result === "PASS" ? "ok" : result === "WARN" ? "warn" : "solid-bad"}>{result}</Pill>;
}

/** Small decision pill. Hold with high risk reads as red. */
export function DecisionPill({ decision, risk, long = false }: { decision: Decision; risk?: Risk | null; long?: boolean }) {
  if (decision === "Approve") return <Pill tone="ok">Approve</Pill>;
  if (decision === "Reject") return <Pill tone="bad">Reject</Pill>;
  if (risk === "high") return <Pill tone="bad">Hold · high risk</Pill>;
  return <Pill tone="warn">{long ? "Hold · needs review" : "Hold"}</Pill>;
}

/** Pill for the "expected result" text on sample cards ("Approve", "Hold", "Hold · high risk"). */
export function ExpectedPill({ text }: { text: string }) {
  if (text.startsWith("Approve")) return <Pill tone="ok">{text}</Pill>;
  return <Pill tone={text.includes("high risk") ? "bad" : "warn"}>{text}</Pill>;
}

export function ConfidencePill({ value }: { value: number }) {
  return <Pill tone={value >= 0.8 ? "ok" : "warn"}>{value.toFixed(2)}</Pill>;
}

export function DecisionBadge({ decision, risk }: { decision: Decision; risk?: Risk | null }) {
  const highRiskHold = decision === "Hold" && risk === "high";   // same rule as the small pill in the header
  const tones = { Approve: "bg-[#DCFAE6] text-[#085D3A]", Hold: "bg-[#FEF0C7] text-[#93370D]", Reject: "bg-[#FEE4E2] text-[#912018]" };
  const tone = highRiskHold ? tones.Reject : tones[decision];
  return <span className={`inline-block rounded-lg px-3.5 py-[7px] text-[15px] font-semibold uppercase tracking-[.04em] ${tone}`}>{highRiskHold ? "Hold · high risk" : decision}</span>;
}
