import type { Agent, Artifact, Graph, Overview, PipelineEvent } from "./types";

/** Relative, always: in dev Vite proxies /api, in production the Python process
 *  serves this bundle from the same origin. Neither case wants a base URL. */
async function get<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: { accept: "application/json" } });
  if (!response.ok) {
    throw new Error(`${path} → ${response.status} ${response.statusText}`);
  }
  return (await response.json()) as T;
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const parsed = (await response.json()) as { detail?: string };
      if (parsed.detail) detail = parsed.detail;
    } catch {
      /* the body was not JSON; the status line will have to do */
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export const api = {
  overview: () => get<Overview>("/api/overview"),
  graph: () => get<Graph>("/api/graph"),
  artifacts: () => get<{ artifacts: Artifact[] }>("/api/artifacts"),
  agents: () => get<{ agents: Agent[] }>("/api/agents"),
  log: (limit = 100) => get<{ log: PipelineEvent[]; cursor: number }>(`/api/log?limit=${limit}`),

  pause: () => post<{ paused: boolean }>("/api/control/pause"),
  resume: () => post<{ paused: boolean }>("/api/control/resume"),
  cancel: (job: string) => post<{ job: string; state: string }>(`/api/jobs/${job}/cancel`),
  dispatch: (chat: string, objective: string) =>
    post<{ job: string; state: string }>("/api/dispatch", { chat, objective }),
};
