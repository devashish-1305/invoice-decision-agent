import { useEffect, useState } from "react";
import { api, type Reference as Ref } from "../api";
import { ErrorBox, Skeleton } from "../components/Common";
import { Pill } from "../components/Pills";
import { inr, pct } from "../format";

export default function Reference() {
  const [data, setData] = useState<Ref | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { api.reference().then(setData).catch((e) => setError(e.message)); }, []);

  return (
    <div>
      <h1 className="text-2xl font-semibold text-ink">Reference data</h1>
      <p className="mt-1 text-[14px] text-muted">What every invoice is matched against. Read-only.</p>
      {error && <div className="mt-4"><ErrorBox>{error}</ErrorBox></div>}
      {!data && !error && <div className="mt-6 space-y-4"><Skeleton className="h-48 w-full" /><Skeleton className="h-56 w-full" /></div>}
      {data && (
        <>
          <section className="card mt-6 !p-0" aria-label="Vendors">
            <h2 className="section-title p-4 pb-2">Vendors</h2>
            <table className="w-full text-[13px]">
              <thead><tr className="border-y border-divider text-[11px] uppercase tracking-[.04em] text-muted">
                <th className="px-4 py-2 font-semibold">Vendor</th><th className="px-2 py-2 font-semibold">GSTIN</th><th className="px-2 py-2 font-semibold">State</th><th className="px-2 py-2 font-semibold">Bank account</th><th className="px-4 py-2 font-semibold">Status</th>
              </tr></thead>
              <tbody>
                {data.vendors.map((v) => (
                  <tr key={v.vendor_id} className="border-b border-divider last:border-0">
                    <td className="px-4 py-3 text-ink">{v.name}<span className="ml-2 font-mono text-[11px] text-muted">{v.vendor_id}</span></td>
                    <td className="px-2 py-3 font-mono text-[12px] text-ink">{v.gstin}</td>
                    <td className="px-2 py-3 text-body">{v.state}</td>
                    <td className="px-2 py-3 font-mono text-[12px] text-ink">{v.bank_account}<span className="ml-2 text-muted">{v.ifsc}</span></td>
                    <td className="px-4 py-3"><Pill tone={v.status === "active" ? "ok" : "bad"}>{v.status}</Pill></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          <section className="card mt-5 !p-0" aria-label="Purchase orders">
            <h2 className="section-title p-4 pb-2">Purchase orders</h2>
            <table className="w-full text-[13px]">
              <thead><tr className="border-y border-divider text-[11px] uppercase tracking-[.04em] text-muted">
                <th className="px-4 py-2 font-semibold">PO</th><th className="px-2 py-2 font-semibold">Vendor</th><th className="px-2 py-2 text-right font-semibold">PO subtotal</th><th className="px-2 py-2 text-right font-semibold">Approved to date</th><th className="px-4 py-2 text-right font-semibold">Utilisation</th>
              </tr></thead>
              <tbody>
                {data.pos.map((p) => (
                  <tr key={p.po_id} className="border-b border-divider last:border-0">
                    <td className="px-4 py-3 font-mono text-[12px] font-medium text-accent-text">{p.po_id}</td>
                    <td className="px-2 py-3 text-ink">{p.vendor}</td>
                    <td className="px-2 py-3 text-right font-mono text-ink">{inr(p.subtotal, false)}</td>
                    <td className="px-2 py-3 text-right font-mono text-ink">{inr(p.approved_to_date, false)}</td>
                    <td className={`px-4 py-3 text-right font-mono ${Number(p.utilisation_pct) > 100 ? "font-semibold text-bad" : "text-ink"}`}>{pct(p.utilisation_pct)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </>
      )}
    </div>
  );
}
