import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api/client";
import { useEventStream } from "./api/useEventStream";
import { Backstage } from "./views/Backstage";
import { SharedSpace } from "./views/SharedSpace";

import "./styles/tokens.css";
import "./styles/shared.css";
import "./styles/canvas.css";

type Tab = "out" | "wf";

export default function App() {
  const [tab, setTab] = useState<Tab>("out");
  const [paused, setPaused] = useState(false);
  const [toast, setToast] = useState("");
  const toastTimer = useRef<number>();

  // A second, lightweight subscription purely for the connection lamp. The
  // Backstage view holds the one that actually drives the canvas.
  const wire = useEventStream(useCallback(() => {}, []));

  const notify = useCallback((message: string) => {
    setToast(message);
    window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(""), 3200);
  }, []);

  // The canvas is dark, the monitoring view is on paper. The body class is what
  // swaps the whole palette, exactly as in the mockup.
  useEffect(() => {
    document.body.classList.toggle("is-back", tab === "wf");
  }, [tab]);

  useEffect(() => {
    void api.overview().then((o) => setPaused(o.paused)).catch(() => undefined);
  }, [tab]);

  const togglePause = useCallback(async () => {
    try {
      const result = paused ? await api.resume() : await api.pause();
      setPaused(result.paused);
      notify(
        result.paused
          ? "Paused. Nothing will be sent until you resume."
          : "Resumed. Sending is live again.",
      );
    } catch (error) {
      notify(String(error));
    }
  }, [paused, notify]);

  return (
    <>
      <header className="top">
        <div className="mark">
          <span className="dot" />
          Plug<small>quiet agents</small>
        </div>
        <nav className="tabs" role="tablist">
          <span className={`wire ${wire === "live" ? "live" : wire === "down" ? "down" : ""}`}>
            {wire}
          </span>
          <button
            className="tab"
            role="tab"
            aria-selected={tab === "out"}
            onClick={() => setTab("out")}
          >
            Shared space
          </button>
          <button
            className="tab"
            role="tab"
            aria-selected={tab === "wf"}
            onClick={() => setTab("wf")}
          >
            Backstage
          </button>
          <button className={`btn ${paused ? "warm" : ""}`} onClick={togglePause}>
            {paused ? "Resume" : "Pause"}
          </button>
        </nav>
      </header>

      <main className={`page ${tab === "out" ? "on" : ""}`}>
        {tab === "out" && <SharedSpace notify={notify} />}
      </main>
      <main className={`page ${tab === "wf" ? "on" : ""}`}>
        {tab === "wf" && <Backstage live={wire === "live"} />}
      </main>

      <div className={`toast ${toast ? "up" : ""}`}>{toast}</div>
    </>
  );
}
