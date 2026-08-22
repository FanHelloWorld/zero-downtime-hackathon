/**
 * Where each node sits when the canvas first draws.
 *
 * Fixed rather than auto-laid-out because the pipeline is fixed and reads best
 * left-to-right, in the order work actually moves. Workers are the exception —
 * there can be any number of them — so they stack in a column that grows
 * downward. Everything is draggable afterwards; this is only the starting point.
 */

// Nodes are 210px wide, so the column pitch has to exceed that or the edges have
// nowhere to be drawn and the graph reads as a row of touching boxes.
const PITCH = 320;

export const POSITIONS: Record<string, { x: number; y: number }> = {
  chat: { x: 0, y: 260 },
  watchdog: { x: PITCH, y: 260 },
  spool: { x: PITCH * 2, y: 260 },
  supervisor: { x: PITCH * 3, y: 260 },
  planner: { x: PITCH * 4, y: 40 },
  agent: { x: PITCH * 4, y: 260 },
  messages: { x: PITCH * 6, y: 150 },
  brightdata: { x: PITCH * 6, y: 470 },
};

const WORKER_COLUMN_X = PITCH * 5;
const WORKER_TOP_Y = 260;
const WORKER_GAP = 150;


export function positionFor(id: string, workerIndex: number): { x: number; y: number } {
  return POSITIONS[id] ?? { x: WORKER_COLUMN_X, y: WORKER_TOP_Y + workerIndex * WORKER_GAP };
}
