// react-grid-layout wrapper — arranges widgets on a movable/resizable grid.
// Dragging only via the `.widget-drag` header; widgets are fixed while editing is off.

import GridLayout, { WidthProvider, type Layout } from "react-grid-layout";
import { WIDGETS } from "./registry";
import type { WidgetInstance } from "./types";

const Grid = WidthProvider(GridLayout);

export function DashboardGrid({
  widgets,
  layout,
  editMode,
  onLayoutChange,
  onCloseWidget,
  onUpdateWidget,
}: {
  widgets: WidgetInstance[];
  layout: Layout[];
  editMode: boolean;
  onLayoutChange: (l: Layout[]) => void;
  onCloseWidget: (i: string) => void;
  onUpdateWidget: (i: string, patch: Partial<WidgetInstance>) => void;
}) {
  return (
    <Grid
      className="layout"
      layout={layout}
      cols={12}
      rowHeight={28}
      margin={[10, 10]}
      containerPadding={[0, 0]}
      isDraggable={editMode}
      isResizable={editMode}
      draggableHandle=".widget-drag"
      compactType="vertical"
      onLayoutChange={onLayoutChange}
      resizeHandles={["se"]}
    >
      {widgets.map((w) => {
        const meta = WIDGETS[w.type];
        const lay = layout.find((l) => l.i === w.i);
        return (
          <div
            key={w.i}
            data-grid={
              lay ?? {
                x: 0,
                y: Infinity,
                w: meta.size.w,
                h: meta.size.h,
                minW: meta.size.minW,
                minH: meta.size.minH,
              }
            }
          >
            {meta.render({
              instance: w,
              onClose: () => onCloseWidget(w.i),
              update: (patch) => onUpdateWidget(w.i, patch),
            })}
          </div>
        );
      })}
    </Grid>
  );
}
