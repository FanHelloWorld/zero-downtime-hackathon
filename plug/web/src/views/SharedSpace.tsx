import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import type { Artifact, Overview } from "../api/types";

const FILTERS: Array<[string, string]> = [
  ["all", "Everything"],
  ["proposal", "Considered"],
  ["ready", "Finished"],
  ["retired", "Closed"],
];

function ago(seconds?: number): string {
  if (seconds === undefined) return "";
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

function ArtifactCard({
  artifact,
  onCancel,
  onDispatch,
}: {
  artifact: Artifact;
  onCancel: (job: string) => void;
  onDispatch: (chat: string, objective: string) => void;
}) {
  const proposal = artifact.kind === "proposal";
  return (
    <article className={`card ${artifact.kind}`}>
      <div className="c-top">
        <span className="c-name">{artifact.title}</span>
        <span className="c-agent">{artifact.agent}</span>
        <span className={`c-state ${proposal ? "warm" : artifact.kind === "retired" ? "off" : ""}`}>
          {artifact.state}
          {artifact.age_seconds !== undefined && ` · ${ago(artifact.age_seconds)}`}
        </span>
      </div>

      {proposal ? (
        <p className="prov">
          {artifact.read || "The planner thought a lookup would help here."}
          {artifact.tone && <> The room read as <q>{artifact.tone}</q>.</>}
        </p>
      ) : (
        <p className="prov">
          Filed for <b>{artifact.chat}</b>
          {artifact.attempts ? ` after ${artifact.attempts} attempt(s)` : ""}.
        </p>
      )}

      {(artifact.reply || artifact.note || artifact.missing?.length) && (
        <dl className="payload">
          {artifact.reply && (
            <div className="row">
              <dt>Said</dt>
              <dd>{artifact.reply}</dd>
            </div>
          )}
          {artifact.note && (
            <div className="row">
              <dt>Note</dt>
              <dd>{artifact.note}</dd>
            </div>
          )}
          {artifact.missing && artifact.missing.length > 0 && (
            <div className="row">
              <dt>Still unknown</dt>
              <dd>{artifact.missing.join("; ")}</dd>
            </div>
          )}
        </dl>
      )}

      <div className="wake">
        <span className="cond">{artifact.chat}</span>
        <span className="acts">
          {proposal ? (
            <button
              className="btn warm"
              onClick={() => onDispatch(artifact.chat, artifact.title)}
              title="Starts a lookup now. Its answer will be posted to the chat."
            >
              Look it up
            </button>
          ) : (
            artifact.state !== "delivered" && (
              <button className="btn" onClick={() => onCancel(artifact.id)}>
                Stop waiting
              </button>
            )
          )}
        </span>
      </div>
    </article>
  );
}

export function SharedSpace({ notify }: { notify: (message: string) => void }) {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [filter, setFilter] = useState("all");

  const reload = useCallback(async () => {
    const [summary, cards] = await Promise.all([api.overview(), api.artifacts()]);
    setOverview(summary);
    setArtifacts(cards.artifacts);
  }, []);

  useEffect(() => {
    void reload();
    const timer = window.setInterval(() => void reload(), 5000);
    return () => window.clearInterval(timer);
  }, [reload]);

  const onCancel = useCallback(
    async (job: string) => {
      try {
        await api.cancel(job);
        notify("Stopped. It will not deliver anything.");
        await reload();
      } catch (error) {
        notify(String(error));
      }
    },
    [notify, reload],
  );

  const onDispatch = useCallback(
    async (chat: string, objective: string) => {
      try {
        const job = await api.dispatch(chat, objective);
        notify(`Looking it up — ${job.job}. The answer posts to the chat.`);
        await reload();
      } catch (error) {
        notify(String(error));
      }
    },
    [notify, reload],
  );

  const shown = useMemo(
    () => artifacts.filter((a) => filter === "all" || a.kind === filter),
    [artifacts, filter],
  );

  const peak = Math.max(1, ...(overview?.histogram ?? [1]));

  return (
    <>
      <section className="quiet">
        <div className="quiet-head">
          <h1>What it heard, and how little it said</h1>
          <p className="sub">
            The agent reads every burst and speaks almost never. These two numbers are
            supposed to be far apart — the gap is the feature.
          </p>
        </div>

        <div className="meter">
          <div className="bars">
            {(overview?.histogram ?? []).map((value, index) => (
              <i
                key={index}
                className={value > peak * 0.6 ? "hot" : ""}
                style={{ height: `${Math.max(2, (value / peak) * 62)}px` }}
              />
            ))}
          </div>
          <div className="baseline" />
        </div>

        <div className="meter-legend">
          <span>
            <b>{overview?.heard ?? 0}</b> messages heard, last{" "}
            {Math.round(overview?.window_hours ?? 72)}h
          </span>
          <span className="key k-quiet">
            Read the room and stayed quiet <b>{overview?.stayed_quiet ?? 0}</b>
          </span>
          <span className="key k-spoke">
            Actually spoke <b>{overview?.spoke ?? 0}</b>
          </span>
          <span>
            Lookups finished <b>{overview?.finished ?? 0}</b>
          </span>
          {overview?.dry_run && <span className="pill">dry run — nothing is sent</span>}
          {overview && overview.dry_run_verified === false && (
            <span className="pill">supervisor unreachable — send state unconfirmed</span>
          )}
          {overview?.paused && <span className="pill">paused</span>}
        </div>
      </section>

      <div className="shell">
        <aside className="rail">
          <h3>Filter</h3>
          {FILTERS.map(([key, label]) => {
            const count =
              key === "all" ? artifacts.length : artifacts.filter((a) => a.kind === key).length;
            return (
              <button
                className="filt"
                key={key}
                aria-pressed={filter === key}
                onClick={() => setFilter(key)}
              >
                {label} <em>{count}</em>
              </button>
            );
          })}
          <p className="rail-note">
            A <em>considered</em> card is something the planner thought worth looking up and
            then didn't. Nothing acts on it unless you do.
          </p>
        </aside>

        <section className="stream">
          {shown.length === 0 && (
            <p className="empty">
              Nothing yet. Text the chat it is watching and this fills in.
            </p>
          )}
          {shown.map((artifact) => (
            <ArtifactCard
              key={artifact.id}
              artifact={artifact}
              onCancel={onCancel}
              onDispatch={onDispatch}
            />
          ))}
        </section>
      </div>
    </>
  );
}
