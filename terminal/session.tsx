import { useCallback, useSyncExternalStore, type ReactNode } from "react";

import { EmptyState, PaneStatusBody, usePaneFooter, type PaneFooterSegment } from "gloomberb/components";
import { useAsyncResource, useAutoRefresh, useUpdatedAgo } from "gloomberb/react";
import { Box } from "gloomberb/ui";

import { qsClient, type SessionState } from "./client";

/** Engine data moves every decision bar; a monitor refreshes well inside one. */
export const REFRESH_MS = 15_000;

export function useQsSession(): SessionState {
  return useSyncExternalStore(
    (listener) => qsClient.subscribe(listener),
    () => qsClient.getState(),
  );
}

/**
 * One qsdash read, refreshed on the host's visibility-aware cadence and
 * dropped to null while there is no session. `path` is the API path after
 * /api; a change of path is a new resource.
 */
export function useQsResource<T>(path: string | null, intervalMs: number = REFRESH_MS) {
  const session = useQsSession();
  const loader = useCallback(
    (_force: boolean) => qsClient.get<T>(path!),
    // the session's identity is part of the resource: a new login refetches
    [path, session.connected, session.baseUrl, session.username],
  );
  const resource = useAsyncResource<T>(session.connected && path ? loader : null);
  useAutoRefresh(resource.updatedAt, resource.load, { intervalMs });
  return resource;
}

/** The footer every quantsys pane shares: loading, error and data age. */
export function useQsFooter(
  paneId: string,
  state: { loading: boolean; error: string | null; updatedAt: number | null },
  extra: PaneFooterSegment[] = [],
): void {
  const session = useQsSession();
  const age = useUpdatedAgo(state.updatedAt);
  // callers build `extra` inline, so its content, not its identity, is the dep
  const extraKey = extra.map((s) => `${s.id}:${s.parts.map((p) => p.text).join(" ")}`).join("|");
  usePaneFooter(paneId, () => {
    const info: PaneFooterSegment[] = [];
    if (session.connected && session.baseUrl) {
      info.push({ id: "host", parts: [{ text: new URL(session.baseUrl).host, tone: "muted" }] });
    }
    if (state.loading) info.push({ id: "loading", parts: [{ text: "loading", tone: "muted" }] });
    if (state.error) info.push({ id: "error", parts: [{ text: state.error, tone: "warning" }] });
    else if (age) info.push({ id: "age", parts: [{ text: age, tone: "muted" }] });
    return { info: [...info, ...extra] };
  }, [session.connected, session.baseUrl, state.loading, state.error, age, extraKey]);
}

/**
 * The body every quantsys pane wraps its content in: a connect prompt while
 * there is no session, the host's loading and error states until the first
 * payload, then the content.
 */
export function QsBody({ width, height, loading, error, hasData, children }: {
  width: number;
  height: number;
  loading: boolean;
  error: string | null;
  hasData: boolean;
  children: ReactNode;
}) {
  const session = useQsSession();
  if (!session.connected) {
    return (
      <Box flexDirection="column" width={width} height={height} padding={1}>
        <EmptyState
          title="Not connected to quantsys."
          message={session.error ?? undefined}
          hint="Open the command bar and run QSC (Connect to quantsys)."
        />
      </Box>
    );
  }
  return (
    <PaneStatusBody
      width={width}
      height={height}
      loading={loading && !hasData}
      error={error && !hasData ? error : null}
      errorTitle="qsdash request failed."
      subject="quantsys"
    >
      {children}
    </PaneStatusBody>
  );
}
