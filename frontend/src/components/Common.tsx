/** Small shared pieces: loading skeleton, error box, confirm dialog. */
import { useEffect, useRef, type ReactNode } from "react";

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse rounded-md bg-divider ${className}`} aria-hidden />;
}

export function ErrorBox({ children }: { children: ReactNode }) {
  return <div role="alert" className="rounded-xl border border-bad-line bg-bad-bg px-4 py-3 text-[14px] text-bad">{children}</div>;
}

/** An amber notice: something unfinished, but nothing that undoes a recorded decision. */
export function Notice({ children }: { children: ReactNode }) {
  return <div role="status" className="rounded-xl border border-[#FEC84B] bg-warn-bg px-4 py-3 text-[14px] text-warn">{children}</div>;
}

export function ConfirmDialog(props: { open: boolean; title: string; body: string; confirmLabel: string; busy?: boolean; onConfirm: () => void; onCancel: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (props.open && !d.open) d.showModal();
    if (!props.open && d.open) d.close();
  }, [props.open]);
  return (
    <dialog ref={ref} onCancel={props.onCancel} className="w-[420px] rounded-xl border border-line p-0 shadow-xl backdrop:bg-black/30">
      <div className="p-5">
        <h2 className="section-title">{props.title}</h2>
        <p className="mt-2 text-[14px] text-body">{props.body}</p>
        <div className="mt-5 flex justify-end gap-2">
          <button type="button" className="btn-outline" onClick={props.onCancel}>Cancel</button>
          <button type="button" disabled={props.busy} onClick={props.onConfirm}
            className="inline-flex items-center rounded-lg bg-bad px-3.5 py-2 text-[13px] font-medium text-white hover:opacity-90 disabled:opacity-60">
            {props.busy ? "Working…" : props.confirmLabel}
          </button>
        </div>
      </div>
    </dialog>
  );
}
