import type { Metadata, Viewport } from "next";
import "./globals.css";
import { SITE } from "@/lib/config";

export const metadata: Metadata = {
  title: {
    default: SITE.title,
    template: `%s · ${SITE.title}`,
  },
  description: SITE.description,
  icons: {
    icon: "/favicon.svg",
  },
  openGraph: {
    title: SITE.title,
    description: SITE.description,
    type: "website",
  },
};

export const viewport: Viewport = {
  themeColor: "#005CB9",
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="de">
      <body>{children}</body>
    </html>
  );
}
