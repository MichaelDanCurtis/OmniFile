import { createContext, use, useCallback, useMemo, useState, type ReactNode } from "react";
import { CheckIcon, InfoIcon, XIcon } from "./icons";

interface Toast {
  id: number;
  message: ReactNode;
  tone: "ok" | "info";
}

const ToastContext = createContext<(message: ReactNode, tone?: Toast["tone"]) => void>(() => {});

let seq = 0;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const dismiss = useCallback(
    (id: number) => setToasts((all) => all.filter((t) => t.id !== id)),
    [],
  );
  const notify = useCallback(
    (message: ReactNode, tone: Toast["tone"] = "ok") => {
      const id = ++seq;
      setToasts((all) => [...all.slice(-3), { id, message, tone }]);
      setTimeout(() => dismiss(id), 6000);
    },
    [dismiss],
  );
  const value = useMemo(() => notify, [notify]);
  return (
    <ToastContext value={value}>
      {children}
      <div className="toast-region" role="status" aria-live="polite">
        {toasts.map((toast) => (
          <div key={toast.id} className="toast">
            {toast.tone === "ok" ? <CheckIcon /> : <InfoIcon />}
            <div>{toast.message}</div>
            <button
              type="button"
              className="icon-btn"
              aria-label="Dismiss"
              onClick={() => dismiss(toast.id)}
            >
              <XIcon size={14} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext>
  );
}

export function useToast() {
  return use(ToastContext);
}
