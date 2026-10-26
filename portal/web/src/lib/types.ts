/**
 * API types — mirrors the Rust axum portal responses 1:1.
 * (portal/src/main.rs, feeds.rs, intercom.rs, polls.rs)
 */

export interface Service {
  id: string;
  name: string;
  description: string;
  url: string;
}

export interface Announcement {
  level: "info" | "warn";
  text: string;
}

export interface FeedEntry {
  title: string;
  url: string;
  /** ISO-8601 (RFC 3339) when the source provides one. */
  date: string | null;
  /** Human-readable feed name (the source host). */
  source: string;
}

export interface FeedColumnPayload {
  id: string;
  title: string;
  entries: FeedEntry[];
}

export interface FeedResponse {
  columns: FeedColumnPayload[];
}

export interface Attachment {
  url: string;
  name: string;
  size: number | null;
  content_type: string | null;
}

export interface IntercomMessage {
  text: string;
  attachment: Attachment | null;
}

export interface IntercomListResponse {
  messages: IntercomMessage[];
}

export interface PollOption {
  option: string;
  count: number;
}

export interface Poll {
  id: number;
  question: string;
  options: string[];
  votes: number[];
  closed: boolean;
  attachment: Attachment | null;
}

export interface PollListResponse {
  polls: Poll[];
}

export interface AiChatRequest {
  question: string;
}

export interface AiChatResponse {
  answer: string;
}
