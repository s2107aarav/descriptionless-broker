import { useRef, useState } from "react";
import type { ReactNode } from "react";
import { STATUS_LABELS, type Status } from "../lib/gateway";
import { errorText } from "../lib/useGateway";

export function label(value: string) {
  if (Object.hasOwn(STATUS_LABELS, value)) return STATUS_LABELS[value as Status];
  const words = value.replace(/[-_]/g, " ");
  return words ? words[0].toUpperCase() + words.slice(1) : "";
}

export function Outcome({ value }: { value: string }) {
  const tone = value.replaceAll("-", "_");
  return <span className={`status-pill ${tone}`}><span aria-hidden="true" />{label(value)}</span>;
}

export function Evidence({ value, title }: { value: unknown; title: string }) {
  return <pre className="payload workbench-payload" tabIndex={0} aria-label={title}>
    {typeof value === "string" ? value : JSON.stringify(value ?? null, null, 2)}
  </pre>;
}

export function EvidenceDisclosure({ title, value }: { title: string; value: unknown }) {
  return <details className="workbench-disclosure">
    <summary>{title}</summary>
    <Evidence title={title} value={value} />
  </details>;
}

export function Panel({ title, accessory, children }: { title: string; accessory?: ReactNode; children: ReactNode }) {
  return <section className="console-panel">
    <div className="panel-heading"><h2>{title}</h2>{accessory}</div>
    {children}
  </section>;
}

export function Issue({ error }: { error: string }) {
  return error ? <p className="banner-error workbench-error" role="alert">{error}</p> : null;
}

export function useAction() {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const active = useRef(false);

  async function run(name: string, operation: () => Promise<void>) {
    if (active.current) return;
    active.current = true;
    setError("");
    setBusy(name);
    try {
      await operation();
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      setBusy(null);
      active.current = false;
    }
  }

  return { busy, error, run, clearError: () => setError("") };
}
