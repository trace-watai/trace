/**
 * The concise lead of a failure title
 * i.e. the task title without the appended symptom headline. Falls back to the
 * full (trimmed) title when there is no such separator.
 */
export const briefFailureTitle = (title: string): string => {
  const [lead] = title.split(": ");
  const brief = lead.trim();
  return brief.length > 0 ? brief : title.trim();
};
