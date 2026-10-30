import { useEffect, useRef, useState, type DragEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError, type Sample } from "../api";
import { ErrorBox, Skeleton } from "../components/Common";
import { ShieldIcon, UploadIcon } from "../components/Icons";
import { ExpectedPill } from "../components/Pills";
import { inr } from "../format";

const MAX_BYTES = 10 * 1024 * 1024;

export default function NewRun() {
  const navigate = useNavigate();
  const [samples, setSamples] = useState<Sample[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => { api.samples().then(setSamples).catch((e) => setLoadError(e.message)); }, []);

  async function start(label: string, begin: () => Promise<{ run_id: number }>) {
    setBusy(label);
    try {
      const { run_id } = await begin();
      navigate(`/runs/${run_id}`);
    } catch (e) {
      setUploadError(e instanceof ApiError ? e.message : "Something went wrong");
      setBusy(null);
    }
  }

  function upload(file: File | undefined) {
    setUploadError(null);
    if (!file) return;
    if (file.type !== "application/pdf" && !file.name.toLowerCase().endsWith(".pdf")) return setUploadError("Only PDF files are accepted.");
    if (file.size > MAX_BYTES) return setUploadError("The file is larger than 10 MB.");
    start("upload", () => api.startUpload(file));
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    setDragging(false);
    upload(e.dataTransfer.files[0]);
  }

  return (
    <div>
      <h1 className="text-2xl font-semibold text-ink">Process an invoice</h1>
      <p className="mt-1 text-[14px] text-muted">Each invoice is checked against the vendor master, open purchase orders and payment controls.</p>

      <label
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={onDrop}
        className={`mt-6 flex cursor-pointer flex-col items-center rounded-xl border-[1.5px] border-dashed p-10 text-center ${dragging ? "border-accent bg-accent-bg" : "border-[#C7CBD4] bg-white"}`}
      >
        <input ref={input} type="file" accept="application/pdf,.pdf" className="sr-only" aria-label="Choose an invoice PDF" onChange={(e) => { upload(e.target.files?.[0]); e.target.value = ""; }} />
        <UploadIcon width={32} height={32} className="text-accent" />
        <span className="mt-3 text-[15px] font-medium text-ink">{busy === "upload" ? "Uploading…" : "Drop an invoice PDF here, or click to browse"}</span>
        <span className="mt-1 font-mono text-[12px] text-muted">PDF only · up to 10 MB · max 5 pages</span>
      </label>
      {uploadError && <div className="mt-3"><ErrorBox>{uploadError}</ErrorBox></div>}

      <h2 className="section-title mt-9">Sample invoices</h2>
      <p className="mt-0.5 text-[13px] text-muted">Six prepared scenarios. Each runs the full pipeline live.</p>
      {loadError && <div className="mt-4"><ErrorBox>{loadError}</ErrorBox></div>}
      <div className="mt-4 grid gap-3.5" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))" }}>
        {!samples && !loadError && [0, 1, 2, 3, 4, 5].map((i) => <Skeleton key={i} className="h-[190px]" />)}
        {samples?.map((s) => (
          <article key={s.code} className="card flex flex-col">
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="font-mono text-[12px] font-semibold text-muted">{s.code}</p>
                <h3 className="mt-0.5 text-[15px] font-semibold text-ink">{s.title}</h3>
              </div>
              <div className="shrink-0 text-right">
                <p className="label mb-1 !text-[10px]">Expected result</p>
                <ExpectedPill text={s.expected} />
              </div>
            </div>
            <p className="mt-2 flex-1 text-[13px] text-body">{s.description}</p>
            <div className="mt-3 flex items-center justify-between gap-3 rounded-lg bg-page px-3 py-2">
              <div className="min-w-0 text-[12px]">
                <span className={`font-mono font-medium ${s.po ? "text-accent-text" : "text-muted"}`}>{s.po ?? "No PO printed"}</span>
                <span className="block truncate text-muted">{s.vendor}</span>
              </div>
              <div className="shrink-0 text-right">
                <span className="font-mono text-[13px] font-medium text-ink">{inr(s.subtotal, false)}</span>
                <span className="block text-[11px] text-muted">excl. GST</span>
              </div>
            </div>
            <button type="button" disabled={busy !== null} onClick={() => start(s.code, () => api.startSample(s.code))} className="btn-secondary mt-3 w-full disabled:opacity-60">
              {busy === s.code ? "Starting…" : "Run sample"}
            </button>
          </article>
        ))}
      </div>

      <div className="card mt-8 flex items-start gap-3 !p-4">
        <ShieldIcon className="mt-0.5 shrink-0 text-accent" />
        <p className="text-[13px] text-body"><strong className="font-semibold text-ink">How decisions are made:</strong> AI reads the document. Deterministic rules make the payment decision. Every decision shows its evidence.</p>
      </div>
    </div>
  );
}
