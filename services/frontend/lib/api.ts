// The only module in this app that talks to the backend. Every network call the frontend makes
// goes through one of the two functions below.
//
// Phase 7: the browser talks to the Go gateway (services/gateway), never to the orchestrator
// directly -- the gateway is what rate limits, redacts PII, and times upstream calls out (see
// docs/adr/0007-go-python-split.md). Its routes are prefixed /v1 and it proxies POST /query as
// POST /v1/query, POST /query/stream as POST /v1/query/stream, and GET /sources/status as
// GET /v1/sources/status. NEXT_PUBLIC_ORCHESTRATOR_URL (the Phase 6 variable, pointed straight at
// the orchestrator) is retired in favor of NEXT_PUBLIC_GATEWAY_URL -- keeping both would leave it
// ambiguous which one a given request actually used, so this file reads only the new variable now.

const GATEWAY_URL =
  process.env.NEXT_PUBLIC_GATEWAY_URL?.replace(/\/$/, "") ?? "http://localhost:8080";

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

// app/schemas.py::BrokenSource (Phase 7). `status` is kept as `string`, not a literal union, for
// the same forward-compatibility reason ResponseType above is: a value this frontend does not yet
// know about must render as plain text, never crash.
export interface BrokenSource {
  source_url: string;
  status: string;
  consecutive_failures: number;
  last_error: string | null;
  last_http_status: number | null;
  last_success_at: string | null;
}

export interface SourcesStatus {
  as_of: string;
  source_count: number;
  oldest_verified_at: string | null;
  newest_verified_at: string | null;
  stale_source_count: number;
  age_hours: number | null;
  freshness_state: FreshnessState;
  broken_source_count: number;
  broken_sources: BrokenSource[];
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
  const response = await fetch(`${GATEWAY_URL}/v1/query/stream`, {
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

/** GET /v1/sources/status (via the gateway): the header's "live sources" trust indicator. */
export async function getSourcesStatus(signal?: AbortSignal): Promise<SourcesStatus> {
  const response = await fetch(`${GATEWAY_URL}/v1/sources/status`, { signal });
  if (!response.ok) {
    throw new Error(`/v1/sources/status returned HTTP ${response.status}`);
  }
  return (await response.json()) as SourcesStatus;
}
