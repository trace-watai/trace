import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

interface ErrorPanelProps {
  title: string;
  message: string;
}

/**
 * Shared "explicit missing/malformed state" panel — not-yet-bundled,
 * not-yet-verified, corrupt artifact JSON, etc. Every run view should show
 * this instead of a blank page or invented data.
 */
export const ErrorPanel = ({ title, message }: ErrorPanelProps) => (
  <Card className="border-destructive/40 bg-destructive/5">
    <CardHeader>
      <CardTitle className="text-base text-destructive">{title}</CardTitle>
    </CardHeader>
    <CardContent className="text-sm text-muted-foreground">
      {message}
    </CardContent>
  </Card>
);
