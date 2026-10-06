import { LabeledBadge } from "@/components/ui/labeled-badge";

interface BlocksReleaseBadgeProps {
  blocksRelease: boolean;
}

/** Red when the result gates a release, muted when it doesn't. */
export const BlocksReleaseBadge = ({
  blocksRelease,
}: BlocksReleaseBadgeProps) => (
  <LabeledBadge
    label="blocks release"
    value={blocksRelease ? "Yes" : "No"}
    valueClassName={
      blocksRelease ? "bg-red-600 text-white" : "bg-muted text-muted-foreground"
    }
  />
);
