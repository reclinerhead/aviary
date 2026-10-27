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
  title: "The Aviary",
  description:
    "Earl's birdwatch: the yard's life list, live arrivals, the recordings, and the weather station.",
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
