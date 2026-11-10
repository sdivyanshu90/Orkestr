import { useMemo, type ReactElement } from 'react';
import ReactFlow, {
  Background,
  BackgroundVariant,
  Controls,
  MarkerType,
  type Edge,
  type Node,
  type NodeTypes,
} from 'reactflow';
import 'reactflow/dist/style.css';
import type { GraphNode, GraphResponse, TaskNodeData } from '../types';
import { layoutDag } from '../lib/layout';
import TaskNode from './TaskNode';

const nodeTypes: NodeTypes = { task: TaskNode };

interface DagViewProps {
  graph: GraphResponse | null;
  selectedTaskId: string | null;
  onSelectTask: (taskId: string) => void;
}

function toData(node: GraphNode): TaskNodeData {
  const states = node.verification?.criterion_states ?? {};
  const criteriaCount = node.criteria.filter((criterion) => {
    const state = states[criterion.id];
    return state === 'executed_passed' || state === 'advisory';
  }).length;

  return {
    taskId: node.id,
    title: node.title,
    role: node.role,
    status: node.status,
    attempts: node.attempts,
    tokens: node.tokens_used,
    cost: node.cost_usd,
    level: node.verification?.level ?? null,
    criteriaCount,
    criteriaTotal: node.criteria.length,
    dependsOn: node.depends_on,
    errorClass: null,
  };
}

export default function DagView({
  graph,
  selectedTaskId,
  onSelectTask,
}: DagViewProps): ReactElement {
  const { nodes, edges } = useMemo(() => {
    if (!graph) return { nodes: [] as Node<TaskNodeData>[], edges: [] as Edge[] };

    const ids = graph.nodes.map((node) => node.id);
    const dependsOn = new Map<string, readonly string[]>(
      graph.nodes.map((node) => [node.id, node.depends_on] as const),
    );
    const positions = layoutDag(ids, dependsOn);

    const flowNodes: Node<TaskNodeData>[] = graph.nodes.map((node) => {
      const position = positions.get(node.id) ?? { x: 0, y: 0 };
      return {
        id: node.id,
        type: 'task',
        position,
        data: toData(node),
        selected: node.id === selectedTaskId,
        draggable: true,
      };
    });

    const flowEdges: Edge[] = graph.edges.map((edge) => ({
      id: `${edge.source}->${edge.target}`,
      source: edge.source,
      target: edge.target,
      type: 'smoothstep',
      markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 },
      style: { stroke: '#42505f', strokeWidth: 1.4 },
      animated: false,
    }));

    return { nodes: flowNodes, edges: flowEdges };
  }, [graph, selectedTaskId]);

  if (!graph) {
    return (
      <div className="panel-empty">
        <p className="muted small">No graph data for this run.</p>
      </div>
    );
  }

  if (graph.nodes.length === 0) {
    return (
      <div className="panel-empty">
        <p className="empty-title">No tasks in this run</p>
        <p className="muted small">
          The planner has not produced a task graph yet, or the run has no tasks.
        </p>
      </div>
    );
  }

  return (
    <div className="dag-wrap">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodeClick={(_event, node) => onSelectTask(node.id)}
        fitView
        fitViewOptions={{ padding: 0.15 }}
        minZoom={0.15}
        maxZoom={2}
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={18} size={1} color="#232b35" />
        <Controls showInteractive={false} />
      </ReactFlow>
      <p className="dag-hint muted small">
        Edge direction is dependency order: an arrow points from a prerequisite to the task
        that depends on it. Click a node to inspect the task.
      </p>
    </div>
  );
}
