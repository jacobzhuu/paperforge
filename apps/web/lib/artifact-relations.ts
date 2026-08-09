import type { ExportArtifact, VisualSummary } from './types';

export function latestArtifact(
  artifacts: ExportArtifact[],
  format?: ExportArtifact['format'],
): ExportArtifact | undefined {
  return artifacts
    .filter((artifact) => !format || artifact.format === format)
    .sort(
      (left, right) =>
        Date.parse(right.created_at ?? '') - Date.parse(left.created_at ?? ''),
    )[0];
}

export function artifactPredatesVisuals(
  artifact: ExportArtifact | undefined,
  visuals: VisualSummary,
): boolean {
  const artifactAt = Date.parse(artifact?.created_at ?? '');
  return timestampPredatesVisuals(artifactAt, visuals);
}

export function timestampPredatesVisuals(
  artifactAt: number | undefined,
  visuals: VisualSummary,
): boolean {
  const approvedAt = Date.parse(visuals.latest_approved_at ?? '');
  return Boolean(
    artifactAt !== undefined &&
      Number.isFinite(artifactAt) &&
      Number.isFinite(approvedAt) &&
      approvedAt > artifactAt,
  );
}
