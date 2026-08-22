import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  ReactFlow,
  type Edge,
  type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import { api } from "../api/client";
import { useEventStream } from "../api/useEventStream";
import type { Agent, PipelineEvent } from "../api/types";
import { PipelineNode } from "../nodes/PipelineNode";
import { positionFor } from "../nodes/layout";
import { emptyPipeline, pipelineReducer } from "../state/pipeline";

const nodeTypes = { pipeline: PipelineNode };
const TICK_MS = 250;
const LOG_LIMIT = 60;

/** Colour the log line by what kind of moment it was. */
function toneFor(event: PipelineEvent): string {
  const key = `${event.stage}/${event.event}`;
  if (key.startsWith("worker/")) return "lamp";
  if (key === "agent/planned") return "moss";
  if (key === "send/delivered" || key === "send/dry_run") return "lamp";
  if (event.stage === "safety" || key.includes("fail") || key.includes("error")) return "ember";
  return "";
}

function summarise(event: PipelineEvent): string {
  const bits = Object.entries(event.detail)
    .filter(([key]) => !["note", "would_send", "objective"].includes(key))
    .slice(0, 3)
    .map(([key, value]) => `${key}=${String(value).slice(0, 28)}`);
  return bits.join(" · ");
}

export function Backstage({ live }: { live: boolean }) {
  const [pipeline, dispatch] = useReducer(pipelineReducer, emptyPipeline);
  const [log, setLog] = useState<PipelineEvent[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const seenWorkers = useRef<Set<string>>(new Set());

  // Seed from the API so a refresh mid-run rebuilds the canvas instead of
  // starting from an empty one.
  const reload = useCallback(async () => {
    const [graph, recent, roster] = await Promise.all([
      api.graph(),
      api.log(LOG_LIMIT),
      api.agents(),
    ]);
    dispatch({ type: "graph", graph });
    setLog(recent.log);
    setAgents(roster.agents);
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const onEvent = useCallback(
    (event: PipelineEvent) => {
      dispatch({ type: "event", event, now: Date.now() });
      setLog((previous) => [event, ...previous].slice(0, LOG_LIMIT));

      // A worker that has just appeared is not in the graph payload yet. Refetch
      // so its card in the sidebar and its objective on the node catch up.
      const job = event.detail?.job;
      if (typeof job === "string" && !seenWorkers.current.has(job)) {
        seenWorkers.current.add(job);
        void reload();
      }
    },
    [reload],
  );

  useEventStream(onEvent);

  // The decay clock. One interval for the whole canvas; the reducer returns the
  // same object when nothing aged out, so a quiet tick costs no render.
  useEffect(() => {
    const timer = window.setInterval(
      () => dispatch({ type: "tick", now: Date.now() }),
      TICK_MS,
    );
    return () => window.clearInterval(timer);
  }, []);

  const nodes = useMemo<Node[]>(() => {
    let workerIndex = 0;
    return Object.values(pipeline.nodes).map((node) => {
      const isWorker = node.id.startsWith("worker:");
      const position = positionFor(node.id, isWorker ? workerIndex++ : 0);
      return {
        id: node.id,
        type: "pipeline",
        position,
        data: node as unknown as Record<string, unknown>,
        draggable: true,
      };
    });
  }, [pipeline.nodes]);

  const edges = useMemo<Edge[]>(
    () =>
      Object.values(pipeline.edges).map((edge) => ({
        id: edge.id,
        source: edge.source,
        target: edge.target,
        animated: edge.active,
        style: {
          stroke: edge.active ? "var(--lamp)" : "var(--haze)",
          strokeWidth: edge.active ? 2 : 1.4,
          transition: "stroke 0.45s ease",
        },
      })),
    [pipeline.edges],
  );

  const liveCount = Object.values(pipeline.nodes).filter(
    (node) => node.status === "active" || node.status === "running",
  ).length;

  return (
    <div className="wf">
      <aside className="wf-side">
        <h3>Agents</h3>
        {agents.map((agent) => (
          <div className={`ag ${agent.status}`} key={agent.name}>
            <div className="ag-n">{agent.name}</div>
            <div className="ag-m">{agent.note}</div>
          </div>
        ))}
      </aside>

      <section className="wf-main">
        <div className="wf-head">
          <h2>Backstage</h2>
          <span className="pill">{liveCount} lit</span>
          <span className="pill">{live ? "streaming" : "reconnecting"}</span>
          <p>
            The planner produces nothing; it reads the room on every burst and stays quiet.
            When the agent delegates, a worker node appears here because a thread really
            was spawned. Drag anything — the layout is yours, the wiring is not.
          </p>
        </div>

        <div className="canvas-wrap">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            fitView
            fitViewOptions={{ padding: 0.18 }}
            minZoom={0.2}
            maxZoom={1.6}
            proOptions={{ hideAttribution: true }}
          >
            <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="var(--haze)" />
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>

        <div className="log">
          <h3>
            <span>Background log</span>
            <span>{pipeline.lastId ? `#${pipeline.lastId}` : ""}</span>
          </h3>
          <ol>
            {log.map((event, index) => (
              <li key={event.id} className={index === 0 ? "fresh" : ""}>
                <time>{event.iso.slice(11, 19)}</time>
                <span className={toneFor(event)}>
                  <b>
                    {event.stage}/{event.event}
                  </b>{" "}
                  {summarise(event)}
                </span>
              </li>
            ))}
          </ol>
        </div>
      </section>
    </div>
  );
}
