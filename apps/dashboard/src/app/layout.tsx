import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "TRACE Dashboard",
  description: "Trace replay & failure UX for agent runs.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <body>{children}</body>
    </html>
  );
}
