import { describe, expect, it } from "vitest";
import type { Graph, PipelineEvent } from "../api/types";
import { DECAY_MS, emptyPipeline, pipelineReducer, type PipelineState } from "./pipeline";

const GRAPH: Graph = {
  nodes: [
    { id: "chat", kind: "trigger", name: "Messages", desc: "", status: "idle" },
    { id: "watchdog", kind: "agent", name: "Watchdog", desc: "", status: "always" },
    { id: "spool", kind: "queue", name: "Pool", desc: "", status: "idle" },
    { id: "supervisor", kind: "agent", name: "Supervisor", desc: "", status: "always" },
    { id: "planner", kind: "plan", name: "Planner", desc: "", status: "always" },
    { id: "agent", kind: "decides", name: "Agent", desc: "", status: "always" },
    { id: "brightdata", kind: "mcp", name: "Bright Data", desc: "", status: "idle" },
    { id: "messages", kind: "output", name: "Sent", desc: "", status: "idle" },
  ],
  edges: [
    { id: "chat->watchdog", source: "chat", target: "watchdog" },
    { id: "watchdog->spool", source: "watchdog", target: "spool" },
    { id: "spool->supervisor", source: "spool", target: "supervisor" },
    { id: "supervisor->agent", source: "supervisor", target: "agent" },
    { id: "agent->messages", source: "agent", target: "messages" },
    { id: "supervisor->messages", source: "supervisor", target: "messages" },
  ],
};

let nextId = 1;
function ev(stage: string, event: string, detail: Record<string, unknown> = {}): PipelineEvent {
  return { id: nextId++, ts: 0, iso: "", stage, event, chat: "c", detail };
}

function seeded(): PipelineState {
  nextId = 1;
  return pipelineReducer(emptyPipeline, { type: "graph", graph: GRAPH });
}

function fire(state: PipelineState, event: PipelineEvent, now = 1000): PipelineState {
  return pipelineReducer(state, { type: "event", event, now });
}

describe("seeding", () => {
  it("starts every node at its resting state", () => {
    const state = seeded();
    expect(state.nodes.watchdog.status).toBe("always");
    expect(state.nodes.messages.status).toBe("idle");
  });

  it("does not extinguish a node mid-glow when the graph is refetched", () => {
    let state = fire(seeded(), ev("watchdog", "spooled"));
    expect(state.nodes.watchdog.status).toBe("active");
    state = pipelineReducer(state, { type: "graph", graph: GRAPH });
    expect(state.nodes.watchdog.status).toBe("active");
  });
});

describe("the path a message takes", () => {
  it("lights the watchdog and the edges into the pool", () => {
    const state = fire(seeded(), ev("watchdog", "spooled", { count: 1 }));
    expect(state.nodes.watchdog.status).toBe("active");
    expect(state.edges["chat->watchdog"].active).toBe(true);
    expect(state.edges["watchdog->spool"].active).toBe(true);
    expect(state.nodes.agent.status).toBe("always");
  });

  it("carries through dispatch to the agent", () => {
    let state = fire(seeded(), ev("supervisor", "dispatch"));
    expect(state.edges["spool->supervisor"].active).toBe(true);
    state = fire(state, ev("agent", "start"));
    expect(state.nodes.agent.status).toBe("active");
    expect(state.edges["supervisor->agent"].active).toBe(true);
  });

  it("routes a reply out through the agent and a follow-up through the loop", () => {
    let state = fire(seeded(), ev("send", "dry_run", { follow_up: false }));
    expect(state.edges["agent->messages"].active).toBe(true);
    expect(state.edges["supervisor->messages"]?.active).toBe(false);

    state = fire(state, ev("send", "delivered", { follow_up: true }));
    expect(state.edges["supervisor->messages"].active).toBe(true);
  });

  it("shows staying quiet as activity, because it is", () => {
    const state = fire(seeded(), ev("agent", "planned", { pillar: "flow" }));
    expect(state.nodes.agent.status).toBe("active");
    expect(state.nodes.messages.status).toBe("idle");
  });
});

describe("workers", () => {
  it("creates a node that did not exist when the agent delegates", () => {
    const state = fire(seeded(), ev("agent", "delegated", { job: "j_abc", kind: "food" }));
    const node = state.nodes["worker:j_abc"];
    expect(node).toBeDefined();
    expect(node.status).toBe("active");
    expect(node.kind).toBe("worker · food");
    expect(state.edges["agent->worker:j_abc"].active).toBe(true);
  });

  it("lights Bright Data when the worker reaches for it", () => {
    let state = fire(seeded(), ev("agent", "delegated", { job: "j_abc", kind: "food" }));
    state = fire(state, ev("worker", "mcp", { job: "j_abc", tool: "search_engine" }));
    expect(state.nodes.brightdata.status).toBe("active");
    expect(state.edges["worker:j_abc->brightdata"].active).toBe(true);
  });

  it("hands back to the supervisor when it finishes", () => {
    let state = fire(seeded(), ev("agent", "delegated", { job: "j_abc" }));
    state = fire(state, ev("worker", "ready", { job: "j_abc" }));
    expect(state.edges["worker:j_abc->supervisor"].active).toBe(true);
  });

  it("retires a worker that failed, and keeps it retired", () => {
    let state = fire(seeded(), ev("agent", "delegated", { job: "j_abc" }));
    state = fire(state, ev("worker", "failed", { job: "j_abc" }));
    expect(state.nodes["worker:j_abc"].status).toBe("retired");

    state = fire(state, ev("worker", "mcp", { job: "j_abc" }), 2000);
    expect(state.nodes["worker:j_abc"].status).toBe("retired");
  });

  it("ignores a worker event with no job id rather than inventing a node", () => {
    const state = fire(seeded(), ev("worker", "started", {}));
    expect(Object.keys(state.nodes).filter((id) => id.startsWith("worker:"))).toEqual([]);
  });
});

describe("decay", () => {
  it("returns a node to its resting state once it goes cold", () => {
    let state = fire(seeded(), ev("watchdog", "spooled"), 1000);
    state = pipelineReducer(state, { type: "tick", now: 1000 + DECAY_MS - 1 });
    expect(state.nodes.watchdog.status).toBe("active");

    state = pipelineReducer(state, { type: "tick", now: 1000 + DECAY_MS + 1 });
    expect(state.nodes.watchdog.status).toBe("always");
    expect(state.edges["chat->watchdog"].active).toBe(false);
  });

  it("leaves an idle node idle rather than promoting it", () => {
    const state = pipelineReducer(seeded(), { type: "tick", now: 99_999 });
    expect(state.nodes.messages.status).toBe("idle");
  });

  it("does not resurrect a retired worker", () => {
    let state = fire(seeded(), ev("agent", "delegated", { job: "j_abc" }));
    state = fire(state, ev("worker", "expired", { job: "j_abc" }));
    state = pipelineReducer(state, { type: "tick", now: 99_999 });
    expect(state.nodes["worker:j_abc"].status).toBe("retired");
  });

  it("returns the same object when nothing changed, so React can skip the render", () => {
    const state = seeded();
    expect(pipelineReducer(state, { type: "tick", now: 5000 })).toBe(state);
  });
});

describe("the cursor", () => {
  it("ignores a frame it has already seen", () => {
    const first = ev("watchdog", "spooled");
    let state = fire(seeded(), first, 1000);
    const before = state.nodes.watchdog.lastEventAt;
    state = fire(state, first, 9999);
    expect(state.nodes.watchdog.lastEventAt).toBe(before);
  });

  it("tracks the highest id it has processed", () => {
    let state = seeded();
    state = fire(state, ev("watchdog", "spooled"));
    state = fire(state, ev("agent", "start"));
    expect(state.lastId).toBe(2);
  });
});

describe("a graph refetch landing mid-run", () => {
  it("does not make a just-created worker vanish", () => {
    let state = fire(seeded(), ev("agent", "delegated", { job: "j_new", kind: "food" }));
    expect(state.nodes["worker:j_new"]).toBeDefined();

    // The API has not caught up with the job yet.
    state = pipelineReducer(state, { type: "graph", graph: GRAPH });
    expect(state.nodes["worker:j_new"]).toBeDefined();
    expect(state.edges["agent->worker:j_new"]).toBeDefined();
  });

  it("lets a worker that has gone cold be dropped by the refetch", () => {
    let state = fire(seeded(), ev("agent", "delegated", { job: "j_old" }), 1000);
    state = pipelineReducer(state, { type: "tick", now: 1000 + DECAY_MS + 1 });
    state = pipelineReducer(state, { type: "graph", graph: GRAPH });
    expect(state.nodes["worker:j_old"]).toBeUndefined();
  });
});
