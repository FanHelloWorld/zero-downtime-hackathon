import { useEffect, useRef, useState } from "react";
import type { PipelineEvent } from "./types";

export type WireState = "connecting" | "live" | "down";

/**
 * Subscribe to the console's SSE stream.
 *
 * EventSource is doing the hard part: it reconnects on its own and replays
 * Last-Event-ID, so a laptop that sleeps mid-run resumes at its cursor rather
 * than losing everything it was showing. All this hook adds is parsing, a
 * connection indicator, and a stable callback so a re-render does not tear the
 * socket down and build a new one.
 */
export function useEventStream(onEvent: (event: PipelineEvent) => void): WireState {
  const [wire, setWire] = useState<WireState>("connecting");
  const handler = useRef(onEvent);
  handler.current = onEvent;

  useEffect(() => {
    const source = new EventSource("/api/stream");

    source.addEventListener("open", () => setWire("live"));
    source.addEventListener("error", () => setWire("down"));

    source.addEventListener("pipeline", (raw) => {
      setWire("live");
      try {
        handler.current(JSON.parse((raw as MessageEvent).data) as PipelineEvent);
      } catch {
        /* a malformed frame costs one event, not the stream */
      }
    });

    return () => source.close();
  }, []);

  return wire;
}
