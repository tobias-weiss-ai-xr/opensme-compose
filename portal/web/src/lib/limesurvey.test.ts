import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createLimesurveyClient,
  resolveRpcUrl,
  surveysConfigured,
} from "./limesurvey";

/**
 * The RPC transport is injected (fetchImpl), so no global stubs are needed:
 * every test drives the client through recorded `{method, params, id}`
 * envelopes and asserts against verified LimeSurvey 6 response shapes.
 */

interface RecordedCall {
  url: string;
  body: { method: string; params: unknown[]; id: number };
}

function okEnvelope(result: unknown) {
  return { ok: true, status: 200, json: async () => ({ id: 1, result, error: null }) };
}

/** Queue-based fetch mock: pops one JSON-RPC envelope per call. */
function rpcFetchMock(queue: unknown[]) {
  const calls: RecordedCall[] = [];
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({
      url: String(input),
      body: JSON.parse(String(init?.body)) as RecordedCall["body"],
    });
    const next = queue.shift();
    if (next === undefined) throw new Error("unerwarteter zusätzlicher RPC-Aufruf");
    return okEnvelope(next) as unknown as Response;
  });
  return { impl, calls };
}

function makeClient(fetchImpl: typeof fetch) {
  return createLimesurveyClient({
    rpcUrl: "https://surveys.example/admin/remotecontrol",
    user: "admin",
    password: "secret-pass",
    fetchImpl,
  });
}

const survey = {
  sid: 100000,
  gsid: 1,
  surveyls_title: "Beispiel: Datei-Upload (openSME)",
  startdate: null,
  expires: null,
  active: "Y",
};

describe("createLimesurveyClient", () => {
  it("authenticates once and lists surveys from an array result", async () => {
    const { impl, calls } = rpcFetchMock([
      "SESSION-KEY-1", // get_session_key
      [survey], // list_surveys
    ]);
    const client = makeClient(impl as unknown as typeof fetch);

    const surveys = await client.listSurveys();

    // listSurveys returns the RAW LimeSurvey objects (array result, LS6).
    expect(surveys).toEqual([
      {
        sid: 100000,
        gsid: 1,
        surveyls_title: "Beispiel: Datei-Upload (openSME)",
        startdate: null,
        expires: null,
        active: "Y",
      },
    ]);
    expect(calls).toHaveLength(2);
    expect(calls[0].url).toBe("https://surveys.example/admin/remotecontrol");
    expect(calls[0].body.method).toBe("get_session_key");
    expect(calls[0].body.params).toEqual({ username: "admin", password: "secret-pass" });
    expect(calls[1].body.method).toBe("list_surveys");
    expect(calls[1].body.params).toEqual(["SESSION-KEY-1"]);
  });

  it("parses list_surveys results that arrive JSON-encoded (older LimeSurvey)", async () => {
    const { impl } = rpcFetchMock([
      "SESSION-KEY-1",
      JSON.stringify([{ sid: "100001", surveyls_title: "Alt", active: "N" }]),
    ]);
    const client = makeClient(impl as unknown as typeof fetch);

    // The JSON-encoded string is decoded back into raw survey objects.
    await expect(client.listSurveys()).resolves.toEqual([
      { sid: "100001", surveyls_title: "Alt", active: "N" },
    ]);
  });

  it("normalizes participants: completed_responses before token_count, [] summary means 0", async () => {
    const { impl } = rpcFetchMock([
      "KEY",
      [survey],
      { completed_responses: 3, incomplete_responses: 1, token_count: 9 },
      [survey],
      [], // no responses/tokens tables yet
      [survey],
      { token_count: 5, token_completed: 2 }, // token survey, no responses table
    ]);
    const client = makeClient(impl as unknown as typeof fetch);

    // Sequential: the session key is cached after the first round trip.
    const a = await client.listSurveysWithParticipants();
    const b = await client.listSurveysWithParticipants();
    const c = await client.listSurveysWithParticipants();
    expect(a[0].participants).toBe(3);
    expect(b[0].participants).toBe(0);
    expect(c[0].participants).toBe(5);
  });

  it("surfaces wrong credentials as the LimeSurvey result.status", async () => {
    const { impl, calls } = rpcFetchMock([
      { status: "Invalid user name or password" },
    ]);
    const client = makeClient(impl as unknown as typeof fetch);

    await expect(client.listSurveys()).rejects.toThrow(
      "Invalid user name or password",
    );
    expect(calls).toHaveLength(1); // no retry storm on auth failure
  });

  it("re-authenticates exactly once when the session key expired", async () => {
    const { impl, calls } = rpcFetchMock([
      "OLD-KEY",
      { status: "Invalid session key" },
      "NEW-KEY",
      [survey],
    ]);
    const client = makeClient(impl as unknown as typeof fetch);

    const surveys = await client.listSurveysWithParticipants();
    expect(surveys).toEqual([
      {
        id: 100000,
        title: "Beispiel: Datei-Upload (openSME)",
        status: "active",
        participants: 0,
      },
    ]);
    expect(calls.map((c) => c.body.method)).toEqual([
      "get_session_key",
      "list_surveys",
      "get_session_key",
      "list_surveys",
      "get_summary", // summary RPC after the queue ran dry — degraded below
    ]);
    expect(calls[3].body.params).toEqual(["NEW-KEY"]);
    expect(calls[4].body.params).toEqual(["NEW-KEY", 100000]);
  });

  it("shares one session key across parallel calls (single flight)", async () => {
    const { impl, calls } = rpcFetchMock([
      "KEY",
      [survey],
      [survey],
    ]);
    const client = makeClient(impl as unknown as typeof fetch);

    const [a, b] = await Promise.all([
      client.listSurveys(),
      client.listSurveys(),
    ]);
    expect(a).toEqual(b);
    expect(calls.filter((c) => c.body.method === "get_session_key")).toHaveLength(1);
  });

  it("degrades a broken per-survey summary instead of failing the tile", async () => {
    const { impl } = rpcFetchMock([
      "KEY",
      [{ ...survey, sid: 999999, active: "N", surveyls_title: "" }],
      { status: "Invalid surveyid" },
    ]);
    const client = makeClient(impl as unknown as typeof fetch);

    await expect(client.listSurveysWithParticipants()).resolves.toEqual([
      { id: 999999, title: "Umfrage 999999", status: "closed", participants: 0 },
    ]);
  });
});

describe("surveys env helpers", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("surveysConfigured() reflects SURVEYS_ADMIN_PASSWORD", () => {
    vi.stubEnv("SURVEYS_ADMIN_PASSWORD", "");
    expect(surveysConfigured()).toBe(false);
    vi.stubEnv("SURVEYS_ADMIN_PASSWORD", "s3cret");
    expect(surveysConfigured()).toBe(true);
  });

  it("resolveRpcUrl() prefers SURVEYS_RPC_URL, derives from SURVEYS_URL, then the domain fallback", () => {
    vi.stubEnv("SURVEYS_RPC_URL", "https://ls.example/rpc");
    vi.stubEnv("SURVEYS_URL", "https://surveys.example");
    expect(resolveRpcUrl()).toBe("https://ls.example/rpc");

    vi.stubEnv("SURVEYS_RPC_URL", "");
    expect(resolveRpcUrl()).toBe("https://surveys.example/admin/remotecontrol");

    vi.stubEnv("SURVEYS_URL", "");
    vi.stubEnv("OPENSME_DOMAIN", "smi.example");
    expect(resolveRpcUrl()).toBe("https://surveys.smi.example/admin/remotecontrol");
  });
});
