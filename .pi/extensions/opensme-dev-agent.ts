// opensme-dev-agent.ts — pi extension for the openSME dev-maintenance-bot.
// Registers the agent (com.opensme.agent) and the /status, /heal, /diag commands.
//
// The bot runs as a compose sidecar (monitoring/dev-agent.yml → dev-maintenance-bot).
// - /status: persisted status from the agent's state volume (works anytime)
// - /diag:   status + knowledge-base triage of unhealthy containers
// - /heal:   explicit confirmation, then POST /heal against the agent API
//            (requires serve mode: `docker compose run --rm dev-maintenance-bot -serve`)
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

/** POST /heal against a serve-mode agent. Returns the receipt or an error hint. */
async function heal(action: string, target: string): Promise<string> {
	let base = `http://localhost:${API_PORT}`;
	// If the bot is serving inside the compose network, reach it via a throwaway curl.
	try {
		const ip = execSync(
			`docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' opensme-dev-maintenance-bot`,
			{ encoding: "utf8", timeout: 10_000 },
		).trim();
		if (ip) base = `http://${ip}:${API_PORT}`;
	} catch {
		// fall back to localhost
	}
	const res = await fetch(`${base}/heal`, {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify({ action, target }),
		signal: AbortSignal.timeout(30_000),
	});
	if (!res.ok) return `POST /heal failed: HTTP ${res.status}`;
	return JSON.stringify(await res.json(), null, 2);
}

export default function (pi: ExtensionAPI) {
	// Agent discovery: persist a registration entry so other tooling can find the bot.
	pi.on("session_start", async (_event, ctx) => {
		pi.appendEntry(AGENT_ID, {
			name: "opensme-dev-maintenance-bot",
			image: IMAGE,
			stateVolume: STATE_VOL,
			apiPort: Number(API_PORT),
			commands: ["/status", "/heal", "/diag"],
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
}
