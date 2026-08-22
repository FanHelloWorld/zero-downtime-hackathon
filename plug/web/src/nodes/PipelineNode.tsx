import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { LiveNode } from "../state/pipeline";

/**
 * One box on the canvas.
 *
 * The className is the whole animation: the reducer decides between idle,
 * always, active and retired, and the CSS transition on border/shadow does the
 * fade. Nothing here is timed in JavaScript.
 */
export function PipelineNode({ data }: NodeProps) {
  const node = data as unknown as LiveNode;
  return (
    <div className={`node ${node.status}`} title={node.desc}>
      <Handle type="target" position={Position.Left} />
      <div className="n-kind">{node.kind}</div>
      <div className="n-name">
        <span className="led" />
        {node.name}
      </div>
      {node.desc && <div className="n-desc">{node.desc}</div>}
      {node.tools && node.tools.length > 0 && (
        <div className="tools">
          {node.tools.map((tool) => (
            <span className="tool" key={tool}>
              {tool}
            </span>
          ))}
        </div>
      )}
      <div className="n-last">{node.status === "active" ? node.lastEvent : ""}</div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}
