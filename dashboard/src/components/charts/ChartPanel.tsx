import type { ReactNode } from "react";
import { EmptyState } from "../States";
import { type IconName } from "../Icon";

type Props = {
  id: string;
  title: string;
  sub: string;
  /** Empty: show this instead of a chart. */
  empty?: { icon: IconName; title: string; text: string } | null;
  legend?: ReactNode;
  note?: string;
  /** The same numbers as tables, for anyone who cannot or would rather not read a drawing. */
  tables: { caption: string; head: string[]; rows: (string | number)[][] }[];
  children: ReactNode;
  wide?: boolean;
};

export function ChartPanel({ id, title, sub, empty, legend, note, tables, children, wide }: Props) {
  return (
    <section className={`pui-panel chart-panel${wide ? " chart-wide" : ""}`} aria-labelledby={`${id}-title`} id={id}>
      <div className="pui-panel-header">
        <div className="pui-panel-heading">
          <h2 className="pui-panel-title" id={`${id}-title`}>
            {title}
          </h2>
          <span className="pui-panel-sub">{sub}</span>
        </div>
        {legend}
      </div>
      {empty ? (
        <EmptyState icon={empty.icon} title={empty.title}>
          {empty.text}
        </EmptyState>
      ) : (
        <div className="chart-body">
          {children}
          {note ? <p className="chart-note">{note}</p> : null}
          {tables.map((table) => (
            <details key={table.caption} className="chart-data">
              <summary>{table.caption}</summary>
              <div className="chart-table-wrap" role="region" aria-label={table.caption} tabIndex={0}>
                <table className="pui-table">
                  <caption className="pui-sr-only">{table.caption}</caption>
                  <thead>
                    <tr>
                      {table.head.map((cell, index) => (
                        <th key={cell} className={index > 0 ? "pui-num" : undefined}>
                          {cell}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {table.rows.map((row, index) => (
                      <tr key={index}>
                        {row.map((cell, column) => (
                          <td key={column} className={column === 0 ? "pui-mono" : "pui-num"}>
                            {cell}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          ))}
        </div>
      )}
    </section>
  );
}
