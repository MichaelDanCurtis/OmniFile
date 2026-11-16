import { useEffect, useState } from "react";

/** The value, once it has stopped changing for `delayMs`. */
export function useDebounced<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}

/** Debounces a value compared by its JSON, so a fresh but equal object doesn't restart the timer. */
export function useDebouncedJson<T>(value: T, delayMs: number): T {
  const json = JSON.stringify(value);
  const debouncedJson = useDebounced(json, delayMs);
  const [parsed, setParsed] = useState<{ json: string; value: T }>({ json, value });
  if (parsed.json !== debouncedJson) {
    setParsed({ json: debouncedJson, value: JSON.parse(debouncedJson) as T });
  }
  return parsed.value;
}
