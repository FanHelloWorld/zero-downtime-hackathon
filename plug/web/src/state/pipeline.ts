import type { Graph, GraphNode, NodeStatus, PipelineEvent } from "../api/types";

/**
 * Events in, glow out.
 *
 * The whole animation is one idea: an event names a node and the edge that fed
 * it, both light up, and both fade on a timer. Nothing here schedules anything —
 * `tick` is called from the UI and decides what has gone cold, which keeps the
 * reducer pure and lets the tests advance time by hand.
 *
 * The one piece of real bookkeeping is workers. A node per job is not decoration
 * — the supervisor genuinely spawns a thread per delegated lookup — so
 * `agent/delegated` creates a node that did not exist a moment ago, and
 * `worker/failed` retires it.
 */

export const DECAY_MS = 2500;

export interface LiveNode extends GraphNode {
  /** What it returns to once the glow fades. */
  base: NodeStatus;
  status: NodeStatus;
  lastEventAt: number;
  /** Last thing that happened here, shown under the name. */
  lastEvent?: string;
}

export interface LiveEdge {
  id: string;
  source: string;
  target: string;
  lastEventAt: number;
  active: boolean;
}

export interface PipelineState {
  nodes: Record<string, LiveNode>;
  edges: Record<string, LiveEdge>;
  lastId: number;
}

export const emptyPipeline: PipelineState = { nodes: {}, edges: {}, lastId: 0 };

export type PipelineAction =
  | { type: "graph"; graph: Graph }
  | { type: "event"; event: PipelineEvent; now: number }
  | { type: "tick"; now: number };

const WORKER_KIND = "worker · ";

function workerId(event: PipelineEvent): string | null {
  const job = event.detail?.job;
  return typeof job === "string" && job ? `worker:${job}` : null;
}

/** Which nodes and edges an event lights up. */
export function activations(event: PipelineEvent): { nodes: string[]; edges: string[] } {
  const key = `${event.stage}/${event.event}`;
  const worker = workerId(event);

  switch (key) {
    case "watchdog/spooled":
      return { nodes: ["watchdog", "spool"], edges: ["chat->watchdog", "watchdog->spool"] };
    case "supervisor/dispatch":
      return { nodes: ["supervisor"], edges: ["spool->supervisor"] };
    case "planner/planned":
      return { nodes: ["planner"], edges: ["supervisor->planner"] };
    case "agent/start":
      return { nodes: ["agent"], edges: ["supervisor->agent"] };
    case "agent/planned":
      // Read the room and said nothing — the normal outcome, and worth seeing.
      return { nodes: ["agent"], edges: [] };
    case "agent/delegated":
      return worker
        ? { nodes: ["agent", worker], edges: [`agent->${worker}`] }
        : { nodes: ["agent"], edges: [] };
    case "worker/started":
      return worker ? { nodes: [worker], edges: [`agent->${worker}`] } : { nodes: [], edges: [] };
    case "worker/mcp":
      return worker
        ? { nodes: [worker, "brightdata"], edges: [`${worker}->brightdata`] }
        : { nodes: ["brightdata"], edges: [] };
    case "worker/ready":
      return worker
        ? { nodes: [worker, "supervisor"], edges: [`${worker}->supervisor`] }
        : { nodes: ["supervisor"], edges: [] };
    case "send/delivered":
    case "send/dry_run":
      // A follow-up leaves from the loop; a reply leaves from inside the agent.
      return event.detail?.follow_up
        ? { nodes: ["messages", "supervisor"], edges: ["supervisor->messages"] }
        : { nodes: ["messages", "agent"], edges: ["agent->messages"] };
    case "safety/send_blocked":
      return { nodes: ["messages"], edges: [] };
    default:
      // Unmapped events still light their stage if it happens to be a node.
      return { nodes: [event.stage], edges: [] };
  }
}

/** Events that end a worker's life rather than lighting it up. */
function retires(event: PipelineEvent): string | null {
  const key = `${event.stage}/${event.event}`;
  if (key === "worker/expired" || key === "worker/failed") return workerId(event);
  return null;
}

function spawnWorker(id: string, event: PipelineEvent, now: number): LiveNode {
  const kind = typeof event.detail?.kind === "string" ? event.detail.kind : "worker";
  const objective =
    typeof event.detail?.objective === "string" ? event.detail.objective : "looking something up";
  return {
    id,
    kind: `${WORKER_KIND}${kind}`,
    name: id.replace("worker:", ""),
    desc: objective,
    base: "running",
    status: "active",
    lastEventAt: now,
    lastEvent: `${event.stage}/${event.event}`,
    job: id.replace("worker:", ""),
    chat: event.chat ?? undefined,
  };
}

export function pipelineReducer(state: PipelineState, action: PipelineAction): PipelineState {
  switch (action.type) {
    case "graph": {
      const nodes: Record<string, LiveNode> = {};
      for (const node of action.graph.nodes) {
        const previous = state.nodes[node.id];
        const base = (node.status ?? "idle") as NodeStatus;
        nodes[node.id] = {
          ...node,
          base,
          // A refresh must not extinguish something mid-glow.
          status: previous?.status === "active" ? "active" : base,
          lastEventAt: previous?.lastEventAt ?? 0,
          lastEvent: previous?.lastEvent,
        };
      }
      // A worker the stream just conjured may not be in this payload yet — the
      // refetch can land before the job row is visible to the query. Dropping it
      // would make a node appear and vanish, so a still-glowing worker survives
      // a refresh and ages out through the normal decay instead.
      for (const [id, node] of Object.entries(state.nodes)) {
        if (!nodes[id] && id.startsWith("worker:") && node.status === "active") {
          nodes[id] = node;
        }
      }

      const edges: Record<string, LiveEdge> = {};
      for (const edge of action.graph.edges) {
        const previous = state.edges[edge.id];
        edges[edge.id] = {
          ...edge,
          lastEventAt: previous?.lastEventAt ?? 0,
          active: previous?.active ?? false,
        };
      }
      // Same for the edges that hang off a preserved worker.
      for (const [id, edge] of Object.entries(state.edges)) {
        if (!edges[id] && id.includes("worker:") && nodes[edge.source] && nodes[edge.target]) {
          edges[id] = edge;
        }
      }
      return { ...state, nodes, edges };

    }

    case "event": {
      const { event, now } = action;
      if (event.id <= state.lastId) return state; // a replayed frame is not news

      const nodes = { ...state.nodes };
      const edges = { ...state.edges };

      const retired = retires(event);
      if (retired) {
        const existing = nodes[retired];
        if (existing) {
          nodes[retired] = {
            ...existing,
            base: "retired",
            status: "retired",
            lastEventAt: now,
            lastEvent: `${event.stage}/${event.event}`,
          };
        }
        return { nodes, edges, lastId: event.id };
      }

      const { nodes: hitNodes, edges: hitEdges } = activations(event);

      for (const id of hitNodes) {
        const existing = nodes[id];
        if (!existing) {
          // Only workers appear out of nowhere; anything else is an event for a
          // node this build does not draw, and is ignored rather than invented.
          if (id.startsWith("worker:")) nodes[id] = spawnWorker(id, event, now);
          continue;
        }
        if (existing.base === "retired") continue; // a dead worker stays dead
        nodes[id] = {
          ...existing,
          status: "active",
          lastEventAt: now,
          lastEvent: `${event.stage}/${event.event}`,
        };
      }

      for (const id of hitEdges) {
        const [source, target] = id.split("->");
        // An edge to a worker only exists once that worker does, so fall back to
        // deriving it from the id rather than dropping the activation.
        const existing = edges[id] ?? { id, source, target };
        edges[id] = { ...existing, lastEventAt: now, active: true };
      }


      return { nodes, edges, lastId: event.id };
    }

    case "tick": {
      const { now } = action;
      let changed = false;
      const nodes = { ...state.nodes };
      for (const [id, node] of Object.entries(nodes)) {
        if (node.status === "active" && now - node.lastEventAt > DECAY_MS) {
          nodes[id] = { ...node, status: node.base };
          changed = true;
        }
      }
      const edges = { ...state.edges };
      for (const [id, edge] of Object.entries(edges)) {
        if (edge.active && now - edge.lastEventAt > DECAY_MS) {
          edges[id] = { ...edge, active: false };
          changed = true;
        }
      }
      return changed ? { ...state, nodes, edges } : state;
    }
  }
}
