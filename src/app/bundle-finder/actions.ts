"use server"

interface AnalyzeResult {
  steps: { name: string; status: string; message: string; ts: number }[];
  suspects: { address: string; score: number; count: number; totalAnalyzed: number }[];
  hasBundle: boolean;
  fromCache?: boolean;
}

export async function runBundleAnalysis(
  address: string,
  chainId: string,
  tokenCount: number,
  historyLimit: number
): Promise<AnalyzeResult> {
  const API_URL = "http://8.159.141.123:5000/api/analyze";

  try {
    const response = await fetch(API_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        address,
        chainId,
        desiredTokenCount: tokenCount,
        historyLimit, // Pass this parameter if backend supports it
        scope: "middle", // Default scope
        precision: "precise" // Default precision
      }),
      cache: "no-store", // Ensure fresh data
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.error || `Server error: ${response.status}`);
    }

    const result = await response.json();
    return result;
  } catch (error: any) {
    console.error("Analysis failed:", error);
    throw new Error(error.message || "Failed to connect to analysis server");
  }
}
