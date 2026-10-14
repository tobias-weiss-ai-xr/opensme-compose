// opensme-dev-agent.ts — pi extension for the openSME dev-maintenance-bot.
// Registers the agent (com.opensme.agent) and the /status, /heal, /diag,
// /drafts, /approve, /reject and /feedback commands.
//
// The bot runs as a compose sidecar (monitoring/dev-agent.yml → dev-maintenance-bot).
// - /status:   persisted status from the agent's state volume (works anytime)
// - /diag:     status + knowledge-base triage of unhealthy containers
// - /heal:     explicit confirmation, then POST /heal against the agent API
//              (requires serve mode: `docker compose run --rm dev-maintenance-bot -serve`)
// - /drafts:   HITL visibility — list drafts with lifecycle + major flags
// - /approve:  HITL decision — explicit confirmation, then the draft commits
// - /reject:   HITL decision with a mandatory reason (the feedback signal)
// - /feedback: the loop's aggregate — counts + rejection reasons
import { execSync } from "node:child_process";

import type { ExtensionAPI, ExtensionCommandContext } from "@earendil-works/pi-coding-agent";

const AGENT_ID = "com.opensme.agent";
const IMAGE = process.env.DEV_MAINTENANCE_BOT_IMAGE ?? "opensme-dev-maintenance-bot:latest";
// Compose prefixes named volumes with the project name — the stack's
// verified project name is "opensme-compose" (see scripts/backup.sh), so
// the overlay creates opensme-compose_dev-maintenance-bot-state, NOT the
// bare "opensme_dev-maintenance-bot-state" name. Override via
// DEV_AGENT_STATE_VOL when a custom COMPOSE_PROJECT_NAME is in use.
const STATE_VOL =
	process.env.DEV_AGENT_STATE_VOL ?? "opensme-compose_dev-maintenance-bot-state";
const API_PORT = process.env.DEV_AGENT_API_PORT ?? "8082";

/** Query the agent CLI against its state volume (no running container needed). */
function agentStatus(): string {
	try {
		return execSync(
			`docker run --rm -v ${STATE_VOL}:/var/lib/opensme:ro --entrypoint /usr/local/bin/agent ${IMAGE} -status`,
			{ encoding: "utf8", timeout: 30_000 },
		).trim();
	} catch (e) {
		return `agent unreachable: ${e instanceof Error ? e.message.split("\n")[0] : e}`;
	}
}

/** Resolve the serve-mode bot's API base (compose network IP, else localhost). */
function apiBase(): string {
	try {
		const ip = execSync(
			`docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' opensme-dev-maintenance-bot`,
			{ encoding: "utf8", timeout: 10_000 },
		).trim();
		if (ip) return `http://${ip}:${API_PORT}`;
	} catch {
		// fall back to localhost
	}
	return `http://localhost:${API_PORT}`;
}

/** POST /heal against a serve-mode agent. Returns the receipt or an error hint. */
async function heal(action: string, target: string): Promise<string> {
	const res = await fetch(`${apiBase()}/heal`, {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify({ action, target }),
		signal: AbortSignal.timeout(30_000),
	});
	if (!res.ok) return `POST /heal failed: HTTP ${res.status}`;
	return JSON.stringify(await res.json(), null, 2);
}

/** GET/POST JSON against the serve-mode bot; returns {status, body}.
 *  Never throws — callers render the error. */
async function botFetch(path: string, init?: RequestInit): Promise<{ status: number; body: string }> {
	try {
		const res = await fetch(`${apiBase()}${path}`, {
			...init,
			headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
			signal: AbortSignal.timeout(30_000),
		});
		return { status: res.status, body: await res.text() };
	} catch (e) {
		return { status: 0, body: e instanceof Error ? e.message : String(e) };
	}
}

interface DraftRow {
	id: string;
	kind: string;
	text: string;
	where: string;
	major?: boolean;
	approved: boolean;
	rejected: boolean;
	reason?: string;
}

/** Render GET /drafts with pending decisions first (HITL visibility). */
function renderDrafts(drafts: DraftRow[]): string {
	const pending = drafts.filter((d) => !d.approved && !d.rejected);
	const decided = drafts.filter((d) => d.approved || d.rejected);
	if (drafts.length === 0) return "no drafts";
	const lines: string[] = [];
	for (const d of pending) {
		lines.push(`PENDING ${d.major ? "[MAJOR] " : ""}${d.id} (${d.kind}) → ${d.where}`);
		lines.push(`   ${d.text}`);
	}
	for (const d of decided) {
		lines.push(
			`${d.approved ? "COMMITTED" : "REJECTED"} ${d.id} (${d.kind}) → ${d.where}` +
				(d.reason ? ` — reason: ${d.reason}` : ""),
		);
	}
	if (pending.some((d) => d.major)) {
		lines.push("⚠ major decisions pending — decide explicitly");
	}
	return lines.join("\n");
}

export default function (pi: ExtensionAPI) {
	// Agent discovery: persist a registration entry so other tooling can find the bot.
	pi.on("session_start", async (_event, ctx) => {
		pi.appendEntry(AGENT_ID, {
			name: "opensme-dev-maintenance-bot",
			image: IMAGE,
			stateVolume: STATE_VOL,
			apiPort: Number(API_PORT),
			commands: ["/status", "/heal", "/diag", "/drafts", "/approve", "/reject", "/feedback"],
		});
	});

	pi.registerCommand("status", {
		description: "dev-maintenance-bot: container health from the agent's state volume",
		handler: async (_args, ctx: ExtensionCommandContext) => {
			ctx.ui.notify(`dev-maintenance-bot status:\n${agentStatus()}`, "info");
		},
	});

	pi.registerCommand("heal", {
		description: "dev-maintenance-bot: remediate a container (asks for explicit confirmation)",
		handler: async (args, ctx: ExtensionCommandContext) => {
			// usage: /heal restart <container>
			const [action, ...rest] = (args ?? "").trim().split(/\s+/);
			const target = rest.join(" ");
			if (!action || !target) {
				ctx.ui.notify("usage: /heal restart <container>", "warning");
				return;
			}
			const ok = await ctx.ui.confirm(
				"Execute remediation?",
				`${action} ${target}\nThis asks the agent to run the action (DEV_AGENT_ALLOW_HEAL gates real execution).`,
			);
			if (!ok) {
				ctx.ui.notify("heal refused without confirmation", "warning");
				return;
			}
			ctx.ui.notify(`heal receipt:\n${await heal(action, target)}`, "info");
		},
	});

	pi.registerCommand("diag", {
		description: "dev-maintenance-bot: triage unhealthy containers against the runbook knowledge base",
		handler: async (_args, ctx: ExtensionCommandContext) => {
			const status = agentStatus();
			const unhealthy = status
				.split("\n")
				.filter((l) => l.includes("UNHEALTHY") || l.startsWith("  KB["));
			if (!unhealthy.some((l) => l.includes("UNHEALTHY"))) {
				ctx.ui.notify("diag: no unhealthy containers", "info");
				return;
			}
			// Private fields never render: agent output is already anonymized.
			ctx.ui.notify(`diag triage:\n${unhealthy.join("\n")}`, "warning");
		},
	});

	// ── HITL + feedback loop (agent-feedback) ────────────────────────

	pi.registerCommand("drafts", {
		description:
			"dev-maintenance-bot: list agent drafts with lifecycle state (HITL visibility — decide on what you can see)",
		handler: async (_args, ctx: ExtensionCommandContext) => {
			const { status, body } = await botFetch("/drafts");
			if (status !== 200) {
				ctx.ui.notify(`GET /drafts failed (HTTP ${status}): ${body.slice(0, 200)}`, "error");
				return;
			}
			ctx.ui.notify(`drafts:\n${renderDrafts(JSON.parse(body) as DraftRow[])}`, "info");
		},
	});

	pi.registerCommand("approve", {
		description:
			"dev-maintenance-bot: approve a pending draft — this is the human commit (asks for explicit confirmation)",
		handler: async (args, ctx: ExtensionCommandContext) => {
			const id = (args ?? "").trim().split(/\s+/)[0];
			if (!id) {
				ctx.ui.notify("usage: /approve <draft-id>   (see /drafts)", "warning");
				return;
			}
			// HITL: approving commits the draft — never without explicit confirmation.
			const { status, body } = await botFetch("/drafts");
			if (status === 200) {
				const d = (JSON.parse(body) as DraftRow[]).find((x) => x.id === id);
				if (!d) {
					ctx.ui.notify(`no draft ${id}`, "warning");
					return;
				}
				if (d.rejected) {
					ctx.ui.notify(`draft ${id} was rejected — rejection is final (create a new draft)`, "warning");
					return;
				}
				const ok = await ctx.ui.confirm(
					"Commit this draft?",
					`[${d.major ? "MAJOR " : ""}${d.id}] (${d.kind}) → ${d.where}\n${d.text}\n\nApproval commits and is audited.`,
				);
				if (!ok) {
					ctx.ui.notify("approve aborted — the draft stays inert", "warning");
					return;
				}
			}
			const r = await botFetch(`/draft/${id}/approve`, { method: "POST" });
			if (r.status !== 200) {
				ctx.ui.notify(`approve failed (HTTP ${r.status}): ${r.body.slice(0, 200)}`, "error");
				return;
			}
			ctx.ui.notify(`committed:\n${r.body}`, "info");
		},
	});

	pi.registerCommand("reject", {
		description:
			"dev-maintenance-bot: reject a pending draft WITH a reason — the reason is the feedback signal the agent learns from",
		handler: async (args, ctx: ExtensionCommandContext) => {
			const m = (args ?? "").trim().match(/^(\S+)\s+(.+)$/);
			if (!m) {
				ctx.ui.notify(
					"usage: /reject <draft-id> <reason>   (the reason is mandatory — feedback needs the why)",
					"warning",
				);
				return;
			}
			const [, id, reason] = m;
			// Safe direction: rejection can never commit anything, no confirm needed.
			const r = await botFetch(`/draft/${id}/reject`, {
				method: "POST",
				body: JSON.stringify({ reason }),
			});
			if (r.status !== 200) {
				ctx.ui.notify(`reject failed (HTTP ${r.status}): ${r.body.slice(0, 200)}`, "error");
				return;
			}
			ctx.ui.notify(`rejected ${id} — recorded as feedback:\n${reason}`, "info");
		},
	});

	pi.registerCommand("feedback", {
		description:
			"dev-maintenance-bot: the feedback aggregate — decisions, pending major decisions and every rejection reason",
		handler: async (_args, ctx: ExtensionCommandContext) => {
			const { status, body } = await botFetch("/feedback");
			if (status !== 200) {
				ctx.ui.notify(`GET /feedback failed (HTTP ${status}): ${body.slice(0, 200)}`, "error");
				return;
			}
			const fb = JSON.parse(body) as {
				total: number;
				approved: number;
				rejected: number;
				pending: number;
				major_pending: number;
				reasons: { id: string; kind: string; where: string; reason: string }[];
			};
			const lines = [
				`total=${fb.total} approved=${fb.approved} rejected=${fb.rejected} pending=${fb.pending}` +
					(fb.major_pending > 0 ? ` MAJOR_PENDING=${fb.major_pending}` : ""),
			];
			for (const r of fb.reasons ?? []) {
				lines.push(`✗ ${r.id} (${r.kind}) → ${r.where}: ${r.reason}`);
			}
			if (fb.pending > 0) lines.push("→ /drafts to decide");
			ctx.ui.notify(`feedback loop:\n${lines.join("\n")}`, "info");
		},
	});
}
