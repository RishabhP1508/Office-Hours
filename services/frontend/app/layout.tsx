import type { Metadata } from "next";
import { IBM_Plex_Sans, IBM_Plex_Serif } from "next/font/google";
import Header from "../components/Header";
import "./globals.css";

// Weights 400/500/600 only, loaded through next/font/google -- IBM Plex Serif for answer prose,
// the opening line, the wordmark, and the hero headline; IBM Plex Sans for all interface chrome.
const plexSans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-plex-sans",
  display: "swap",
});

const plexSerif = IBM_Plex_Serif({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-plex-serif",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Office Hours",
  description:
    "Cited answers on F-1, OPT, and H-1B status from official U.S. government sources. Unofficial, not legal advice.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${plexSans.variable} ${plexSerif.variable}`}>
      <body className="font-sans">
        <Header />
        <main>{children}</main>
        <footer className="bg-espresso-2">
          <div className="mx-auto max-w-[1120px] px-7 py-6">
            <p className="text-[13px] leading-[1.7] text-[#B0A192]">
              Not legal advice and not affiliated with USCIS. For your own situation, talk to your
              DSO or a licensed immigration attorney.
            </p>
          </div>
        </footer>
      </body>
    </html>
  );
}
