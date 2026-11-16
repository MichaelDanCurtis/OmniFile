import { useEffect } from "react";

export function usePageTitle(title: string | undefined): void {
  useEffect(() => {
    document.title = title ? `${title} · OmniFile Gateway` : "OmniFile Gateway";
  }, [title]);
}
