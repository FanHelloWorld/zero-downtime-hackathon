/** Payload shapes from the console API. Mirrors console/server.py. */

export interface PipelineEvent {
  id: number;
  ts: number;
  iso: string;
  stage: string;
  event: string;
  chat: string | null;
  detail: Record<string, unknown>;
}

export type NodeStatus = "idle" | "always" | "active" | "retired" | "done" | "running";

export interface GraphNode {
  id: string;
  kind: string;
  name: string;
  desc: string;
  status?: NodeStatus;
  tools?: string[];
  job?: string;
  chat?: string;
  state?: string;
  age_seconds?: number;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
}

export interface Graph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface Overview {
  window_hours: number;
  heard: number;
  stayed_quiet: number;
  spoke: number;
  drafted: number;
  finished: number;
  delegated: number;
  histogram: number[];
  pool: { pending: number; leased: number; dead: number };
  jobs: Record<string, number>;
  sends_last_hour: number;
  dry_run: boolean;
  /** Where dry_run came from: the supervisor that owns the send path, or, when
   *  it could not be reached, this console's own environment. */
  dry_run_source?: "supervisor" | "local-env";
  dry_run_verified?: boolean;
  supervisor_running?: boolean | null;
  paused: boolean;
  chats_seen: number;
  config_source: string;
  model: string;
}

export interface Artifact {
  id: string;
  kind: "ready" | "retired" | "proposal";
  title: string;
  agent: string;
  chat: string;
  state: string;
  age_seconds?: number;
  reply?: string;
  note?: string;
  attempts?: number;
  tone?: string;
  pillar?: string;
  read?: string;
  missing?: string[];
}

export interface Agent {
  name: string;
  status: string;
  note: string;
}
