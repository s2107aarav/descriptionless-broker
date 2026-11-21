import { useEffect, useState } from "react";
import { gateway } from "./gateway";

export function errorText(error: unknown) {
  return error instanceof Error ? error.message : "The request failed.";
}

export function useQuery<T>(path: string | null, token: string | undefined, version: number, delay = 0) {
  type Snapshot = { path: string; token: string; data: T | null; error: string };
  const [snapshot, setSnapshot] = useState<Snapshot>();

  useEffect(() => {
    if (!path || !token) return;
    const query = path;
    const credential = token;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function refresh() {
      try {
        const data = await gateway<T>(query, credential, { signal: controller.signal });
        if (!controller.signal.aborted) setSnapshot({ path: query, token: credential, data, error: "" });
      } catch (error) {
        if (!controller.signal.aborted) {
          setSnapshot(previous => ({
            path: query,
            token: credential,
            data: previous?.path === query && previous.token === credential ? previous.data : null,
            error: errorText(error),
          }));
        }
      } finally {
        if (!controller.signal.aborted && delay > 0) timer = setTimeout(refresh, delay);
      }
    }

    void refresh();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [path, token, version, delay]);

  return path && snapshot?.path === path && snapshot.token === token ? snapshot : { data: null, error: "" };
}
