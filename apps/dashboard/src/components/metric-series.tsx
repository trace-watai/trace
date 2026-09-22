/**
 * One series of the metrics history, drawn as an inline SVG.
 *
 * No charting dependency, because four series of scalars do not need one and a
 * dashboard that has to install a library to draw a line is harder to keep
 * working offline. A point with a null value is a gap rather than a zero, since
 * an empty denominator means nothing was measured.
 */

interface MetricPoint {
  commit: string;
  recordedAt: string;
  value: number | null;
  label: string;
  detail: string;
}

interface MetricSeriesProps {
  points: MetricPoint[];
  format: (value: number | null) => string;
}

const WIDTH = 640;
const HEIGHT = 120;
const PAD = 8;

export const MetricSeries = ({ points, format }: MetricSeriesProps) => {
  const measured = points.filter((point) => point.value !== null);
  const values = measured.map((point) => point.value as number);
  // A flat series would divide by zero on span, so give it a full-height band
  // and draw it down the middle rather than pinning it to an edge.
  const max = values.length > 0 ? Math.max(...values, 0) : 1;
  const min = values.length > 0 ? Math.min(...values, 0) : 0;
  const span = max - min || 1;

  const x = (index: number) =>
    points.length === 1
      ? WIDTH / 2
      : PAD + (index * (WIDTH - 2 * PAD)) / (points.length - 1);
  const y = (value: number) =>
    HEIGHT - PAD - ((value - min) / span) * (HEIGHT - 2 * PAD);

  const path = points
    .map((point, index) =>
      point.value === null ? null : `${x(index)},${y(point.value)}`,
    )
    .filter((entry): entry is string => entry !== null)
    .join(" ");

  const latest = points[points.length - 1];

  return (
    <div className="space-y-3">
      <div className="flex items-baseline gap-3">
        <span className="font-mono text-xl">{format(latest.value)}</span>
        <span className="text-sm text-muted-foreground">{latest.label}</span>
      </div>
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="h-28 w-full"
        role="img"
        aria-label={`${points.length} recorded points, latest ${format(latest.value)}`}
      >
        {path.length > 0 && (
          <polyline
            points={path}
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            className="text-primary"
          />
        )}
        {points.map((point, index) =>
          point.value === null ? null : (
            <circle
              key={point.commit}
              cx={x(index)}
              cy={y(point.value)}
              r="4"
              className="fill-primary"
            >
              <title>
                {`${point.commit.slice(0, 7)} on ${point.recordedAt.slice(0, 10)}\n${point.label}\n${point.detail}`}
              </title>
            </circle>
          ),
        )}
      </svg>
      <p className="text-xs text-muted-foreground">{latest.detail}</p>
    </div>
  );
};
