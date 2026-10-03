import { Icon, type IconName } from "../Icon";

/** The key of a chart. Each item has an icon and a word as well as its colour or line style, so no
 * series is told apart by colour alone. */

export type LegendItem = { label: string; icon: IconName; series?: number; swatch?: string };

export function Legend({ items }: { items: LegendItem[] }) {
  return (
    <ul className="pui-legend chart-legend" aria-label="Key">
      {items.map((item) => (
        <li key={item.label} className="pui-legend-item">
          {item.series ? <span className="pui-legend-key" data-series={item.series} aria-hidden="true" /> : <span className={`legend-swatch ${item.swatch ?? ""}`} aria-hidden="true" />}
          <Icon name={item.icon} size="sm" />
          {item.label}
        </li>
      ))}
    </ul>
  );
}
