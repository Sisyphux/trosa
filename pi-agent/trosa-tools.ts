/**
 * Trosa tools for the Pi Agent runtime.
 *
 * This extension deliberately disables no business rules itself: it only
 * translates Pi tool calls into the authenticated Trosa Gateway contract.
 * CRM writes therefore remain inside the Flask Gateway and its shared
 * transaction/undo implementation.
 */

import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { promises as fs } from "node:fs";
import { relative, resolve, sep } from "node:path";

const GATEWAY_URL = (process.env.TROSA_GATEWAY_URL || "http://127.0.0.1:8080").replace(/\/$/, "");
const GATEWAY_TOKEN = (process.env.TROSA_GATEWAY_TOKEN || "").trim();
const WORKFILES_ROOT = process.env.TROSA_WORKFILES_ROOT ? resolve(process.env.TROSA_WORKFILES_ROOT) : "";
// CRM operation is the default and only normal Pi capability.  File access
// must be explicitly enabled for a separately reviewed use case; it never
// belongs to Hamid's day-to-day CRM assistant context.
const WORKFILES_ENABLED = ["1", "true", "yes", "on"].includes(String(process.env.TROSA_PI_ALLOW_WORKFILES || "").trim().toLowerCase());
const REQUEST_TIMEOUT_MS = Math.max(5000, Math.min(Number(process.env.TROSA_GATEWAY_TIMEOUT_MS || 45000), 120000));

type GatewayResponse = {
	status: number;
	payload: Record<string, any>;
};

function text(value: unknown, fallback = "") {
	return String(value ?? fallback).trim();
}

function compactJson(value: unknown) {
	return JSON.stringify(value, (_key, item) => {
		if (typeof item === "string" && item.length > 4000) return `${item.slice(0, 4000)}…`;
		return item;
	}, 0);
}

function toolResult(payload: unknown, details: Record<string, unknown> = {}) {
	return {
		content: [{ type: "text", text: compactJson(payload) }],
		details,
	};
}

function errorResult(message: string, details: Record<string, unknown> = {}) {
	return toolResult({ success: false, error: { code: "tool_error", message } }, details);
}

function idempotencyKey(action: string, toolCallId: string) {
	const requestId = text(process.env.TROSA_PI_REQUEST_ID, "request").replace(/[^A-Za-z0-9_.:-]/g, "_");
	const callId = text(toolCallId, "call").replace(/[^A-Za-z0-9_.:-]/g, "_");
	return `pi:${requestId}:${action}:${callId}`.slice(0, 200);
}

async function gateway(path: string, method = "GET", body?: Record<string, unknown>, write = false, key = ""): Promise<GatewayResponse> {
	if (!GATEWAY_TOKEN) return { status: 503, payload: { success: false, error: { code: "configuration", message: "Trosa Gateway token is not configured" } } };
	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
	try {
		const headers: Record<string, string> = {
			Authorization: `Bearer ${GATEWAY_TOKEN}`,
			Accept: "application/json",
		};
		if (body !== undefined) headers["Content-Type"] = "application/json";
		if (write && key) headers["Idempotency-Key"] = key;
		const response = await fetch(`${GATEWAY_URL}${path}`, {
			method,
			headers,
			body: body === undefined ? undefined : JSON.stringify(body),
			signal: controller.signal,
		});
		let payload: Record<string, any> = {};
		try {
			payload = await response.json();
		} catch {
			payload = { success: false, error: { code: "invalid_response", message: "Trosa Gateway 返回了无法解析的响应" } };
		}
		return { status: response.status, payload };
	} catch (error: any) {
		const message = error?.name === "AbortError" ? "Trosa Gateway 请求超时" : "Trosa Gateway 暂时不可用";
		return { status: 503, payload: { success: false, error: { code: "unavailable", message } } };
	} finally {
		clearTimeout(timer);
	}
}

function gatewayToolResult(result: GatewayResponse) {
	if (result.status >= 200 && result.status < 300 && result.payload.success !== false) {
		const data = result.payload.data || {};
		const action = data.action;
		if (action) {
			const labels: Record<string, string> = {
				record_communication: "记录客户沟通",
				create_task: "创建跟进提醒",
				complete_task: "完成客户待办",
				update_customer: "更新客户资料",
				update_contact: "更新联系人资料",
				resolve_inbox: "处理 Inbox",
				undo: "撤销 CRM 操作",
			};
			const undone = action.status === "undone";
			return toolResult(result.payload, {
				action_id: action.id,
				action_type: action.type || "undo",
				action_label: labels[action.type] || (undone ? "撤销 CRM 操作" : "已完成 CRM 操作"),
				undo_available: !undone,
			});
		}
		return toolResult(result.payload);
	}
	const error = result.payload.error || {};
	return errorResult(text(error.message, "Trosa Gateway 操作未完成"), { status: result.status, code: text(error.code, "internal_error") });
}

function ensureCustomerId(customerId: unknown) {
	const id = Number(customerId);
	return Number.isInteger(id) && id > 0 ? id : 0;
}

async function safeWorkfilePath(relativePath: string) {
	if (!WORKFILES_ROOT) throw new Error("本地工作文件夹尚未配置");
	const candidate = resolve(WORKFILES_ROOT, relativePath);
	const root = await fs.realpath(WORKFILES_ROOT);
	const real = await fs.realpath(candidate);
	if (real !== root && !real.startsWith(root + sep)) throw new Error("文件路径不在允许的工作文件夹内");
	return real;
}

const searchCustomers = defineTool({
	name: "search_customers",
	label: "Search customers",
	description: "通过 Trosa Gateway 搜索客户。只根据返回的精确客户记录确定身份，不要猜测客户 ID。",
	promptSnippet: "Search Trosa customers before any customer-specific answer or write.",
	parameters: Type.Object({ query: Type.String({ description: "客户姓名、公司名或国家；保留用户原话" }), limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 20 })) }),
	async execute(_id, params) {
		const query = encodeURIComponent(text(params.query).slice(0, 200));
		return gatewayToolResult(await gateway(`/api/gateway/customers?query=${query}&limit=${params.limit || 10}`));
	},
});

const getCustomer = defineTool({
	name: "get_customer",
	label: "Get customer",
	description: "读取一个已由 search_customers 返回的 Trosa 客户详情、主联系人和下一步。",
	parameters: Type.Object({ customer_id: Type.Integer({ minimum: 1, description: "仅使用 Gateway 返回的客户 ID" }) }),
	async execute(_id, params) {
		if (!ensureCustomerId(params.customer_id)) return errorResult("客户 ID 无效");
		return gatewayToolResult(await gateway(`/api/gateway/customers/${params.customer_id}`));
	},
});

const getToday = defineTool({
	name: "get_today",
	label: "Get today",
	description: "读取当前 Trosa 用户今天到期的明确待办。",
	parameters: Type.Object({ limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 30 })) }),
	async execute(_id, params) {
		return gatewayToolResult(await gateway(`/api/gateway/today?limit=${params.limit || 15}`));
	},
});

const searchActivity = defineTool({
	name: "search_activity",
	label: "Search activity",
	description: "读取客户沟通时间线；可按客户 ID 或关键词查询。",
	parameters: Type.Object({ customer_id: Type.Optional(Type.Integer({ minimum: 1 })), query: Type.Optional(Type.String()), limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 30 })) }),
	async execute(_id, params) {
		const query = new URLSearchParams();
		if (params.customer_id) query.set("customer_id", String(params.customer_id));
		if (params.query) query.set("query", text(params.query).slice(0, 200));
		query.set("limit", String(params.limit || 15));
		return gatewayToolResult(await gateway(`/api/gateway/activity?${query.toString()}`));
	},
});

const getInbox = defineTool({
	name: "get_inbox",
	label: "Get inbox",
	description: "读取当前 Trosa 用户需要整理的 Inbox 项目。",
	parameters: Type.Object({ limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 30 })) }),
	async execute(_id, params) {
		return gatewayToolResult(await gateway(`/api/gateway/inbox?limit=${params.limit || 15}`));
	},
});

const recordCommunication = defineTool({
	name: "record_communication",
	label: "Record communication",
	description: "通过 Trosa Gateway 记录一条真实客户沟通。执行前必须先确认客户身份；不确定时停止并返回候选，不要猜测。所有字段按用户事实填写，不要编造联系人或日期。",
	parameters: Type.Object({
		customer_id: Type.Integer({ minimum: 1 }),
		content: Type.String({ minLength: 1, maxLength: 4000 }),
		follow_date: Type.Optional(Type.String({ description: "YYYY-MM-DD；不确定时留空" })),
		direction: Type.Optional(Type.Union([Type.Literal("inbound"), Type.Literal("outbound"), Type.Literal("two_way"), Type.Literal("unknown")])),
		activity_type: Type.Optional(Type.String()),
		contact_id: Type.Optional(Type.Integer({ minimum: 1 })),
		source: Type.Optional(Type.String()),
		source_reference: Type.Optional(Type.String()),
		next_task: Type.Optional(Type.String()),
		next_follow_up: Type.Optional(Type.String({ description: "YYYY-MM-DD；没有明确下一步时留空" })),
	}),
	executionMode: "sequential",
	async execute(toolCallId, params) {
		if (!ensureCustomerId(params.customer_id) || !text(params.content)) return errorResult("客户和沟通内容不能为空");
		const payload = { action: "record_communication", customer_id: params.customer_id, payload: {
			content: text(params.content).slice(0, 4000), follow_date: text(params.follow_date), direction: text(params.direction, "unknown"),
			activity_type: text(params.activity_type, "follow_up"), contact_id: params.contact_id || null, source: text(params.source, "pi_agent"),
			source_reference: text(params.source_reference), next_task: text(params.next_task), next_follow_up: text(params.next_follow_up),
		} };
		return gatewayToolResult(await gateway("/api/gateway/actions", "POST", payload, true, idempotencyKey("record_communication", toolCallId)));
	},
});

const createTask = defineTool({
	name: "create_task",
	label: "Create follow-up",
	description: "通过 Trosa Gateway 创建一个有明确动作和日期的客户待办。日期不明确时先询问，不要猜测。",
	parameters: Type.Object({ customer_id: Type.Integer({ minimum: 1 }), title: Type.String({ minLength: 1, maxLength: 300 }), due_date: Type.String({ description: "YYYY-MM-DD" }), source: Type.Optional(Type.String()), source_reference: Type.Optional(Type.String()) }),
	executionMode: "sequential",
	async execute(toolCallId, params) {
		if (!ensureCustomerId(params.customer_id) || !text(params.title) || !/^\d{4}-\d{2}-\d{2}$/.test(text(params.due_date))) return errorResult("创建待办需要可靠的客户、动作和 YYYY-MM-DD 日期");
		const payload = { action: "create_task", customer_id: params.customer_id, payload: { title: text(params.title).slice(0, 300), due_date: text(params.due_date), source: text(params.source, "pi_agent"), source_reference: text(params.source_reference) } };
		return gatewayToolResult(await gateway("/api/gateway/actions", "POST", payload, true, idempotencyKey("create_task", toolCallId)));
	},
});

const completeTask = defineTool({
	name: "complete_task",
	label: "Complete task",
	description: "通过 Trosa Gateway 完成一个已读取并确认的待办；不要根据自然语言猜测 task_id。",
	parameters: Type.Object({ task_id: Type.Integer({ minimum: 1 }), customer_id: Type.Optional(Type.Integer({ minimum: 1 })), completion_context: Type.Optional(Type.String({ maxLength: 1000 })) }),
	executionMode: "sequential",
	async execute(toolCallId, params) {
		if (!Number.isInteger(params.task_id) || params.task_id < 1) return errorResult("待办 ID 无效");
		const payload = { action: "complete_task", customer_id: params.customer_id || null, payload: { task_id: params.task_id, completion_context: text(params.completion_context), source: "pi_agent" } };
		return gatewayToolResult(await gateway("/api/gateway/actions", "POST", payload, true, idempotencyKey("complete_task", toolCallId)));
	},
});

const undoAction = defineTool({
	name: "undo_action",
	label: "Undo Trosa action",
	description: "撤销最近一次 Pi Agent CRM 写入。只使用 Gateway 返回的 action_id。",
	parameters: Type.Object({ action_id: Type.String({ minLength: 1, maxLength: 120 }) }),
	executionMode: "sequential",
	async execute(_id, params) {
		const actionId = text(params.action_id);
		if (!/^agact_[A-Za-z0-9_-]{16,64}$/.test(actionId)) return errorResult("Agent action ID 无效");
		return gatewayToolResult(await gateway(`/api/gateway/actions/${encodeURIComponent(actionId)}/undo`, "POST", {}, true, idempotencyKey("undo", actionId)));
	},
});

const searchWorkFiles = defineTool({
	name: "search_work_files",
	label: "Search work files",
	description: "只读搜索配置的本地工作文件夹；不读取 CRM 数据库、不写文件，也不访问工作文件夹之外的路径。",
	parameters: Type.Object({ query: Type.String({ minLength: 1, maxLength: 200 }), max_results: Type.Optional(Type.Integer({ minimum: 1, maximum: 20 })) }),
	async execute(_id, params) {
		if (!WORKFILES_ROOT) return errorResult("本地工作文件夹尚未配置");
		const needle = text(params.query).toLocaleLowerCase();
		const maxResults = params.max_results || 10;
		const results: Array<Record<string, string>> = [];
		let visited = 0;
		async function walk(dir: string): Promise<void> {
			if (results.length >= maxResults || visited >= 1200) return;
			let entries: any[] = [];
			try { entries = await fs.readdir(dir, { withFileTypes: true }); } catch { return; }
			for (const entry of entries) {
				if (results.length >= maxResults || visited >= 1200) return;
				if (entry.name.startsWith(".") || ["node_modules", ".git", "__pycache__"].includes(entry.name)) continue;
				const full = resolve(dir, entry.name);
				if (entry.isDirectory()) { await walk(full); continue; }
				if (!entry.isFile()) continue;
				visited += 1;
				const rel = relative(WORKFILES_ROOT, full);
				if (needle && rel.toLocaleLowerCase().includes(needle)) { results.push({ path: rel, match: "filename" }); continue; }
				if (!/\.(txt|md|csv|json|xlsx?|docx?|pdf|html?)$/i.test(entry.name)) continue;
				try {
					const content = await fs.readFile(full, "utf8");
					const index = content.toLocaleLowerCase().indexOf(needle);
					if (index >= 0) results.push({ path: rel, match: content.slice(Math.max(0, index - 80), index + needle.length + 160).replace(/\s+/g, " ") });
				} catch { /* binary or unreadable files are skipped */ }
			}
		}
		await walk(WORKFILES_ROOT);
		return toolResult({ success: true, data: { files: results, truncated: visited >= 1200 } }, { visited });
	},
});

const readWorkFile = defineTool({
	name: "read_work_file",
	label: "Read work file",
	description: "读取 search_work_files 返回的相对路径文件；只读且受工作文件夹边界保护。敏感凭证文件会被拒绝。",
	parameters: Type.Object({ relative_path: Type.String({ minLength: 1, maxLength: 500 }), max_chars: Type.Optional(Type.Integer({ minimum: 1, maximum: 30000 })) }),
	async execute(_id, params) {
		const rel = text(params.relative_path);
		if (/^\.env(?:\.|$)|(^|[/\\])(credentials?|secrets?|\.ssh)([/\\]|$)/i.test(rel)) return errorResult("出于安全原因不能读取该文件");
		try {
			const full = await safeWorkfilePath(rel);
			const content = await fs.readFile(full, "utf8");
			const maxChars = params.max_chars || 20000;
			return toolResult({ success: true, data: { path: relative(WORKFILES_ROOT, full), content: content.slice(0, maxChars), truncated: content.length > maxChars } });
		} catch (error: any) {
			return errorResult(error?.message || "文件读取失败");
		}
	},
});

export default function (pi: ExtensionAPI) {
	const tools = [searchCustomers, getCustomer, getToday, searchActivity, getInbox, recordCommunication, createTask, completeTask, undoAction];
	if (WORKFILES_ENABLED) tools.push(searchWorkFiles, readWorkFile);
	for (const tool of tools) {
		pi.registerTool(tool);
	}
}
