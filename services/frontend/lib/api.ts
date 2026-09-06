// The only module in this app that talks to the backend. Every network call the frontend makes
// goes through one of the two functions below.

const ORCHESTRATOR_URL =
  process.env.NEXT_PUBLIC_ORCHESTRATOR_URL?.replace(/\/$/, "") ?? "http://localhost:8000";

export interface Citation {
  source_url: string;
  chunk_id: number;
  snippet: string;
}

export interface RetrievedContext {
  chunk_id: number;
  source_url: string;
  section_heading: string;
  content: string;
}

export interface FreshnessSource {
  source_url: string;
  page_last_updated: string | null;
  last_verified_at: string;
  fetched_at: string;
  rule_effective_date: string | null;
}

export interface FreshnessNotice {
  source_url: string;
  rule_effective_date: string;
  in_effect: boolean;
  reason: "top_ranked" | "cited";
}

export interface Freshness {
  as_of: string;
  sources: FreshnessSource[];
  notices: FreshnessNotice[];
}

// app/schemas.py::ResponseType. Kept as `string`, not a literal union: a response carrying a
// future value this frontend does not know about yet must fall back to plain prose, never crash
// (see components/Message.tsx).
export type ResponseType =
  | "answer"
  | "refusal_advice"
  | "clarify"
  | "no_answer"
  | "blocked_unverified"
  | (string & {});

export interface AnswerResponse {
  answer: string;
  citations: Citation[];
  contexts: RetrievedContext[];
  disclaimer: string;
  generated_at: string;
  response_type: ResponseType;
  refusal_reason: string | null;
  freshness: Freshness | null;
}

export type FreshnessState = "current" | "recent" | "stale" | "unknown";

export interface SourcesStatus {
  as_of: string;
  source_count: number;
  oldest_verified_at: string | null;
  newest_verified_at: string | null;
  stale_source_count: number;
  age_hours: number | null;
  freshness_state: FreshnessState;
}

export type StageName = "classify" | "retrieve" | "generate" | "verify";

export type StageEvent =
  | { event: "ping" }
  | {
      event: "stage";
      stage: StageName;
      status: "start" | "done";
      source_count?: number;
      ok?: boolean;
    }
  | { event: "result"; response: AnswerResponse }
  | { event: "error"; detail: string };

/**
 * POST /query/stream, read as Server-Sent Events over a plain fetch (not EventSource: EventSource
 * cannot POST a body). Frames are `data: <json>\n\n`; partial frames can arrive split across
 * chunk boundaries, so this buffers across reads and only parses once a full `\n\n`-terminated
 * frame is available. `onEvent` is called once per parsed frame, in arrival order.
 */
export async function streamQuery(
  question: string,
  onEvent: (event: StageEvent) => void,
  signal?: AbortSignal
): Promise<void> {
  const response = await fetch(`${ORCHESTRATOR_URL}/query/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
    signal,
  });

  if (!response.ok || !response.body) {
    throw new Error(`/query/stream returned HTTP ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let separatorIndex: number;
    while ((separatorIndex = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, separatorIndex);
      buffer = buffer.slice(separatorIndex + 2);
      const dataLine = frame.split("\n").find((line) => line.startsWith("data: "));
      if (!dataLine) continue;
      onEvent(JSON.parse(dataLine.slice("data: ".length)) as StageEvent);
    }
  }
}

/** GET /sources/status: the header's "live sources" trust indicator. */
export async function getSourcesStatus(signal?: AbortSignal): Promise<SourcesStatus> {
  const response = await fetch(`${ORCHESTRATOR_URL}/sources/status`, { signal });
  if (!response.ok) {
    throw new Error(`/sources/status returned HTTP ${response.status}`);
  }
  return (await response.json()) as SourcesStatus;
}
