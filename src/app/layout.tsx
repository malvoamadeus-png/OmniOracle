import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import { Sidebar } from "@/components/Sidebar";

const inter = Inter({ subsets: ["latin"] });

export const metadata: Metadata = {
  title: "OmniOracle Dashboard",
  description: "Track copy trading performance",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className={inter.className}>
        <div className="flex h-screen w-full overflow-hidden">
          <Sidebar />
          <div className="flex flex-1 flex-col overflow-hidden">
            <main className="flex-1 overflow-y-auto bg-gray-50/10 p-4 md:p-6">
              {children}
            </main>
          </div>
        </div>
        <Analytics />
      </body>
    </html>
  );
}
