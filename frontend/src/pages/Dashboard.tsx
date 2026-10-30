import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type RunList } from "../api";
import { ConfirmDialog, ErrorBox, Skeleton } from "../components/Common";
import { SearchIcon } from "../components/Icons";
import { DecisionPill } from "../components/Pills";
import { inr, when } from "../format";

type Filter = "All" | "Approved" | "Held" | "Rejected";
const MATCH: Record<Filter, string | null> = { All: null, Approved: "Approve", Held: "Hold", Rejected: "Reject" };

function Tile(props: { label: string; value: string; sub: string; tone?: string; money?: boolean }) {
  return (
    <div className="card !p-4">
      <p className="text-[12px] text-muted">{props.label}</p>
      <p className={`mt-1 font-semibold ${props.money ? "font-mono text-[20px]" : "text-2xl"} ${props.tone ?? "text-ink"}`}>{props.value}</p>
      <p className="mt-0.5 text-[11px] text-muted">{props.sub}</p>
    </div>
  );
}

export default function Dashboard() {
  const navigate = useNavigate();
  const [data, setData] = useState<RunList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("All");
  const [query, setQuery] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [resetting, setResetting] = useState(false);

  const load = useCallback(() => api.runs().then((d) => { setData(d); setError(null); }).catch((e) => setError(e.message)), []);
  useEffect(() => { load(); }, [load]);
  const inFlight = data?.runs.some((r) => r.status === "queued" || r.status === "running");
  useEffect(() => { if (!inFlight) return; const t = setInterval(load, 2000); return () => clearInterval(t); }, [inFlight, load]);

  async function reset() {
    setResetting(true);
    try { await api.reset(); await load(); setConfirming(false); } catch (e) { setError((e as Error).message); setConfirming(false); }
    setResetting(false);
  }

  const counts = useMemo(() => {
    const finished = data?.runs.filter((r) => r.decision && (r.status === "completed" || r.status === "failed")) ?? [];
    return { All: finished.length, Approved: finished.filter((r) => r.decision === "Approve").length, Held: finished.filter((r) => r.decision === "Hold").length, Rejected: finished.filter((r) => r.decision === "Reject").length };
  }, [data]);

  const rows = (data?.runs ?? []).filter((r) => {
    if (MATCH[filter] && r.decision !== MATCH[filter]) return false;
    const q = query.trim().toLowerCase();
    return !q || (r.vendor_name ?? "").toLowerCase().includes(q) || (r.invoice_number ?? "").toLowerCase().includes(q);
  });
  const k = data?.kpis;

  return (
    <div>
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-ink">Dashboard</h1>
          <p className="mt-1 text-[14px] text-muted">Every invoice processed, with its decision and the reason.</p>
        </div>
        <button type="button" className="btn-outline" onClick={() => setConfirming(true)}>Reset demo data</button>
      </div>
      <ConfirmDialog open={confirming} busy={resetting} title="Reset demo data?" confirmLabel="Reset"
        body="This clears the run history and restores the seed vendors, purchase orders and approved invoices."
        onConfirm={reset} onCancel={() => setConfirming(false)} />

      {error && <div className="mt-4"><ErrorBox>{error}</ErrorBox></div>}

      <div className="mt-6 grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))" }}>
        {!k ? [0, 1, 2, 3, 4, 5].map((i) => <Skeleton key={i} className="h-[92px]" />) : (
          <>
            <Tile label="Invoices processed" value={String(k.processed)} sub="since last reset" />
            <Tile label="Approved" value={String(k.approved)} sub="since last reset" tone="text-ok" />
            <Tile label="Held" value={String(k.held)} sub="since last reset" tone="text-warn" />
            <Tile label="Rejected" value={String(k.rejected)} sub="since last reset" tone="text-bad" />
            <Tile label="Auto-approved" value={inr(k.auto_approved, false)} sub="excl. GST" money />
            <Tile label="Stopped for review" value={inr(k.stopped, false)} sub="excl. GST" money />
          </>
        )}
      </div>

      <section className="card mt-5 !p-0" aria-label="Run history">
        <div className="flex items-center justify-between gap-3 border-b border-divider p-3">
          <div role="group" aria-label="Filter by decision" className="inline-flex rounded-lg bg-[#F2F4F7] p-0.5">
            {(Object.keys(MATCH) as Filter[]).map((f) => (
              <button key={f} type="button" aria-pressed={filter === f} onClick={() => setFilter(f)}
                className={`rounded-md px-3 py-1.5 text-[13px] font-medium ${filter === f ? "bg-white text-ink shadow-sm" : "text-muted hover:text-ink"}`}>
                {f} {counts[f]}
              </button>
            ))}
          </div>
          <label className="relative">
            <span className="sr-only">Search by vendor or invoice number</span>
            <SearchIcon width={16} height={16} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted" />
            <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search vendor or invoice no."
              className="w-[250px] rounded-lg border border-[#D0D5DD] bg-white py-1.5 pl-8 pr-3 text-[13px] placeholder:text-muted" />
          </label>
        </div>

        {!data ? <div className="space-y-2 p-4">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-10 w-full" />)}</div>
          : data.runs.length === 0 ? <p className="p-8 text-center text-[14px] text-muted">No runs yet. Start one from <Link to="/" className="text-accent-text underline">New run</Link>.</p>
          : rows.length === 0 ? <p className="p-8 text-center text-[14px] text-muted">No runs match this filter.</p>
          : (
          <table className="w-full text-[13px]">
            <thead><tr className="border-b border-divider text-[11px] uppercase tracking-[.04em] text-muted">
              <th className="px-4 py-2 font-semibold">Time</th><th className="px-2 py-2 font-semibold">Vendor</th><th className="px-2 py-2 font-semibold">Invoice</th>
              <th className="px-2 py-2 text-right font-semibold">Subtotal</th><th className="px-3 py-2 font-semibold">Decision</th><th className="px-4 py-2 font-semibold">Primary reason</th>
            </tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} onClick={() => navigate(`/runs/${r.id}`)} className="cursor-pointer border-b border-divider last:border-0 hover:bg-page">
                  <td className="whitespace-nowrap px-4 py-3 font-mono text-[12px] text-muted">{when(r.created_at)}</td>
                  <td className="px-2 py-3 text-ink">{r.vendor_name ?? <span className="text-muted">Not read</span>}</td>
                  <td className="px-2 py-3"><Link to={`/runs/${r.id}`} onClick={(e) => e.stopPropagation()} className="whitespace-nowrap font-mono text-accent-text hover:underline">{r.invoice_number ?? `Run #${r.id}`}</Link></td>
                  <td className="px-2 py-3 text-right font-mono text-ink">{inr(r.subtotal, false)}</td>
                  <td className="px-3 py-3">{r.decision && (r.status === "completed" || r.status === "failed") ? <DecisionPill decision={r.decision} risk={r.risk_level} /> : <span className="text-[12px] text-accent-text">Running…</span>}</td>
                  <td className="max-w-[280px] px-4 py-3">
                    {r.top_code ? <><span className="block font-mono text-[11px] font-semibold text-ink">{r.top_code}</span><span className="line-clamp-2 text-[12px] text-muted">{r.top_reason}</span></>
                      : r.decision === "Approve" ? <span className="text-[12px] text-muted">All checks passed</span> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
