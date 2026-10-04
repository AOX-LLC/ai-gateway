import { SkeletonKpi, SkeletonLines } from "@/components/States";

/** Shown while a range change reads the database. */
export default function Loading() {
  return (
    <div className="pui-content" aria-busy="true">
      <section className="kpi-grid" aria-label="Loading key metrics">
        {[0, 1, 2, 3].map((i) => (
          <SkeletonKpi key={i} />
        ))}
      </section>
      <div className="chart-grid">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="pui-panel">
            <SkeletonLines rows={6} />
          </div>
        ))}
      </div>
      <div className="panel-grid">
        <div className="pui-panel">
          <SkeletonLines rows={6} />
        </div>
        <div className="pui-panel">
          <SkeletonLines rows={5} />
        </div>
      </div>
    </div>
  );
}
