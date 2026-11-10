/**
 * Left-to-right layered layout for the task DAG.
 *
 * The depth of a node is the length of the longest dependency chain that
 * ends at it, so every dependency edge points strictly left to right.
 */

export interface Point {
  x: number;
  y: number;
}

export const NODE_WIDTH = 264;
export const NODE_HEIGHT = 128;
export const COLUMN_GAP = 72;
export const ROW_GAP = 36;

export function layoutDag(
  nodeIds: readonly string[],
  dependsOn: ReadonlyMap<string, readonly string[]>,
): Map<string, Point> {
  const known = new Set(nodeIds);
  const depth = new Map<string, number>();

  const resolve = (id: string, seen: Set<string>): number => {
    const cached = depth.get(id);
    if (cached !== undefined) return cached;
    if (seen.has(id)) return 0;
    seen.add(id);
    const parents = (dependsOn.get(id) ?? []).filter((parent) => known.has(parent));
    let value = 0;
    parents.forEach((parent) => {
      const parentDepth = resolve(parent, seen);
      if (parentDepth + 1 > value) value = parentDepth + 1;
    });
    depth.set(id, value);
    return value;
  };

  nodeIds.forEach((id) => resolve(id, new Set<string>()));

  const columns = new Map<number, string[]>();
  nodeIds.forEach((id) => {
    const level = depth.get(id) ?? 0;
    const bucket = columns.get(level);
    if (bucket) bucket.push(id);
    else columns.set(level, [id]);
  });

  const positions = new Map<string, Point>();
  columns.forEach((ids, level) => {
    ids.forEach((id, row) => {
      positions.set(id, {
        x: level * (NODE_WIDTH + COLUMN_GAP),
        y: row * (NODE_HEIGHT + ROW_GAP),
      });
    });
  });

  return positions;
}
