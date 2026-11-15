import { execFile } from "node:child_process";
import { broadcastConfig, fanoutBroadcast, mailBroadcast, matrixBroadcast } from "./broadcast";

const { mockExecFile } = vi.hoisted(() => ({
  mockExecFile: vi.fn(),
}));

vi.mock("node:child_process", () => ({
  execFile: mockExecFile,
  default: { execFile: mockExecFile },
}));

const mockedExecFile = vi.mocked(execFile);

const CFG = broadcastConfig({
  MATRIX_DOMAIN: "matrix.test",
  MATRIX_USER: "opus",
  MATRIX_PASSWORD: "sekret",
  MATRIX_ROOM: "#support:matrix.test",
  BROADCAST_FANOUT_SCRIPT: "/repo/scripts/broadcast-fanout.sh",
  BROADCAST_SMTP_HOST: "127.0.0.1",
  BROADCAST_SMTP_PORT: "25",
});

function jsonRes(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Login → alias resolve → send: the happy-path 3-call fetch chain. */
function stubMatrixHappyPath() {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(jsonRes({ access_token: "tok123" })) // login
    .mockResolvedValueOnce(jsonRes({ room_id: "!room:hs" })) // directory
    .mockResolvedValueOnce(jsonRes({ event_id: "$evt1" })); // send
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("broadcastConfig", () => {
  it("defaults match booking-notify.sh / broadcast-fanout.sh without env", () => {
    const cfg = broadcastConfig({});
    expect(cfg.matrixHomeserver).toBe("https://matrix.home.opensme.graphwiz.ai");
    expect(cfg.matrixUser).toBe("testuser");
    expect(cfg.matrixPassword).toBe("eval2026");
    expect(cfg.matrixRoom).toBe("#support:matrix.home.opensme.graphwiz.ai");
    expect(cfg.smtpHost).toBe("stalwart");
    expect(cfg.smtpPort).toBe("25");
    expect(cfg.subject).toBe("[openSME] Broadcast an alle");
    expect(cfg.fanoutScript).toMatch(/scripts[/\\]broadcast-fanout\.sh$/);
  });

  it("E2E_TESTUSER_PASSWORD beats the eval2026 baseline; BROADCAST_* wins where set", () => {
    const cfg = broadcastConfig({
      E2E_TESTUSER_PASSWORD: "from-e2e",
      BROADCAST_MAIL_TO: "a@x.test,b@y.test",
      BROADCAST_SMTP_PORT: "587",
      BROADCAST_SUBJECT: "Sonderbetreff",
    });
    expect(cfg.matrixPassword).toBe("from-e2e");
    expect(cfg.mailTo).toBe("a@x.test,b@y.test");
    expect(cfg.smtpPort).toBe("587");
    expect(cfg.subject).toBe("Sonderbetreff");
  });
});

describe("matrixBroadcast", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("logs in, resolves the alias, sends m.room.message with an idempotent txn id", async () => {
    const fetchMock = stubMatrixHappyPath();

    const res = await matrixBroadcast("Hallöchen", CFG);

    expect(res.ok).toBe(true);
    expect(res.detail).toContain("$evt1");
    expect(res.detail).toContain("!room:hs");

    const [loginUrl, loginInit] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(loginUrl).toBe("https://matrix.test/_matrix/client/v3/login");
    const loginBody = JSON.parse(String(loginInit.body)) as {
      password?: string;
      identifier?: { user?: string };
    };
    expect(loginBody.password).toBe("sekret");
    expect(loginBody.identifier?.user).toBe("opus");

    const [dirUrl, dirInit] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(dirUrl).toBe(
      "https://matrix.test/_matrix/client/v3/directory/room/%23support%3Amatrix.test",
    );
    expect((dirInit.headers as Record<string, string>).Authorization).toBe("Bearer tok123");

    const [sendUrl, sendInit] = fetchMock.mock.calls[2] as [string, RequestInit];
    expect(sendUrl).toMatch(
      /^https:\/\/matrix\.test\/_matrix\/client\/v3\/rooms\/!room%3Ahs\/send\/m\.room\.message\/broadcast-\d+-/,
    );
    expect(JSON.parse(String(sendInit.body))).toEqual({
      msgtype: "m.text",
      body: "Hallöchen",
    });
  });

  it("skips the directory call for a direct '!room_id' target", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonRes({ access_token: "tok" }))
      .mockResolvedValueOnce(jsonRes({ event_id: "$e" }));
    vi.stubGlobal("fetch", fetchMock);

    const res = await matrixBroadcast("x", { ...CFG, matrixRoom: "!direct:hs" });

    expect(res.ok).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(String(fetchMock.mock.calls[1]?.[0])).toContain("/rooms/!direct%3Ahs/send/");
  });

  it("retries a 429 login once, honoring retry_after_ms", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonRes({ retry_after_ms: 1 }, 429))
      .mockResolvedValueOnce(jsonRes({ access_token: "tok2" }))
      .mockResolvedValueOnce(jsonRes({ room_id: "!r" }))
      .mockResolvedValueOnce(jsonRes({ event_id: "$e" }));
    vi.stubGlobal("fetch", fetchMock);

    const res = await matrixBroadcast("x", CFG);
    expect(res.ok).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("reports a failed login without leaking the password", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonRes({ errcode: "M_FORBIDDEN" }, 403)));

    const res = await matrixBroadcast("x", CFG);
    expect(res.ok).toBe(false);
    expect(res.detail).toContain("403");
    expect(res.detail).not.toContain("sekret");
    expect(res.detail).not.toContain("M_FORBIDDEN");
  });

  it("reports a failed send with the HTTP status", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonRes({ access_token: "tok" }))
      .mockResolvedValueOnce(jsonRes({ room_id: "!r" }))
      .mockResolvedValueOnce(jsonRes({ errcode: "M_FORBIDDEN" }, 403));
    vi.stubGlobal("fetch", fetchMock);

    const res = await matrixBroadcast("x", CFG);
    expect(res.ok).toBe(false);
    expect(res.detail).toContain("send failed (HTTP 403)");
  });
});

describe("mailBroadcast", () => {
  beforeEach(() => {
    mockExecFile.mockReset();
  });

  it("shells out to scripts/broadcast-fanout.sh with BROADCAST_* env and reports the outcome", async () => {
    mockExecFile.mockImplementation(
      (
        _file: string,
        _args: readonly string[],
        _opts: never,
        cb: (e: Error | null, stdout: string, stderr: string) => void,
      ) => {
        cb(null, "\u001b[0;32m[OK]\u001b[0m   broadcast delivered to 14 recipient(s)\n", "");
      },
    );

    const res = await mailBroadcast("Guten Morgen", CFG);

    expect(res.ok).toBe(true);
    // ANSI colors stripped, last stdout line kept
    expect(res.detail).toBe("broadcast delivered to 14 recipient(s)");
    expect(mockedExecFile).toHaveBeenCalledTimes(1);
    const [file, args, opts] = mockedExecFile.mock.calls[0] as unknown as [
      string,
      string[],
      { env: Record<string, string>; timeout: number },
    ];
    expect(file).toBe("bash");
    expect(args).toEqual(["/repo/scripts/broadcast-fanout.sh"]);
    expect(opts.env.BROADCAST_BODY).toBe("Guten Morgen");
    expect(opts.env.BROADCAST_SUBJECT).toBe(CFG.subject);
    expect(opts.env.BROADCAST_SMTP_HOST).toBe("127.0.0.1");
    expect(opts.env.BROADCAST_SMTP_PORT).toBe("25");
    // no BROADCAST_MAIL_TO → the script discovers from bootstrap/demo-users.json
    expect(opts.env.BROADCAST_MAIL_TO).toBeUndefined();
    expect(opts.timeout).toBeGreaterThan(0);
  });

  it("passes BROADCAST_MAIL_TO/BROADCAST_MAIL_FROM through when configured", async () => {
    mockExecFile.mockImplementation(
      (
        _file: string,
        _args: readonly string[],
        _opts: never,
        cb: (e: Error | null, stdout: string, stderr: string) => void,
      ) => cb(null, "ok", ""),
    );
    await mailBroadcast("x", {
      ...CFG,
      mailTo: "a@x.test",
      mailFrom: "broadcast@x.test",
    });
    const opts = mockedExecFile.mock.calls[0]?.[2] as { env: Record<string, string> };
    expect(opts.env.BROADCAST_MAIL_TO).toBe("a@x.test");
    expect(opts.env.BROADCAST_MAIL_FROM).toBe("broadcast@x.test");
  });

  it("reports non-zero exits with the stderr tail", async () => {
    mockExecFile.mockImplementation(
      (
        _file: string,
        _args: readonly string[],
        _opts: never,
        cb: (e: Error | null, stdout: string, stderr: string) => void,
      ) => {
        const err = Object.assign(new Error("command failed"), { code: 1 });
        cb(err, "", "\u001b[0;31m[ERR]\u001b[0m  broadcast send failed\n");
      },
    );

    const res = await mailBroadcast("x", CFG);
    expect(res.ok).toBe(false);
    expect(res.detail).toBe("exit 1 — broadcast send failed");
  });
});

describe("fanoutBroadcast", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });
  beforeEach(() => {
    mockExecFile.mockReset();
  });

  it("runs both legs in parallel and stays best-effort: Element down, Mail up", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new TypeError("fetch failed")), // transport fail
    );
    mockExecFile.mockImplementation(
      (
        _file: string,
        _args: readonly string[],
        _opts: never,
        cb: (e: Error | null, stdout: string, stderr: string) => void,
      ) => cb(null, "delivered to 14 recipient(s)", ""),
    );

    const report = await fanoutBroadcast("Hallo", CFG);

    expect(report.element.ok).toBe(false);
    expect(report.element.detail).toContain("fetch failed");
    expect(report.mail.ok).toBe(true);
    expect(report.mail.detail).toBe("delivered to 14 recipient(s)");
  });
});
