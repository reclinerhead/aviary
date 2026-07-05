import type { Metadata } from "next";
import { Fraunces, Sometype_Mono } from "next/font/google";
import "./globals.css";

// Ranger-station type pairing: Fraunces (a soft, slightly wonky naturalist
// serif) for display, Sometype Mono for every piece of telemetry. Self-hosted
// at build time by next/font -- no runtime font requests.
const fraunces = Fraunces({
  variable: "--font-display",
  subsets: ["latin"],
  axes: ["SOFT", "WONK", "opsz"],
});

const sometypeMono = Sometype_Mono({
  variable: "--font-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Merle Control Center",
  description:
    "Field station for the driveway: live wildlife detection, counts, and controls.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${fraunces.variable} ${sometypeMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
