/** Pi extension used only to consume the Trosa MCP Server in local tests. */
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

let clientPromise: Promise<Client> | undefined;

function client() {
  if (!clientPromise) clientPromise = (async () => {
    const transport = new StdioClientTransport({ command: process.execPath, args: [new URL("./trosa-mcp-server.mjs", import.meta.url).pathname], env: {
      PATH: process.env.PATH || "", TROSA_GATEWAY_URL: process.env.TROSA_GATEWAY_URL || "", TROSA_GATEWAY_TOKEN: process.env.TROSA_GATEWAY_TOKEN || "",
      TROSA_GATEWAY_TIMEOUT_MS: process.env.TROSA_GATEWAY_TIMEOUT_MS || "",
    } });
    const value = new Client({ name: "trosa-pi-test-client", version: "1.0.0" });
    await value.connect(transport);
    return value;
  })();
  return clientPromise;
}

function bridge(name: string, label: string, description: string, parameters: any) {
  return defineTool({ name, label, description, parameters, executionMode: name === "record_communication" || name === "create_task" || name === "complete_task" || name === "undo_action" ? "sequential" : undefined,
    async execute(_id, params) {
      try {
        const reply: any = await (await client()).callTool({ name, arguments: params });
        return { content: reply.content || [{ type: "text", text: JSON.stringify(reply) }], details: {} };
      } catch (error: any) { return { content: [{ type: "text", text: JSON.stringify({ success: false, error: { code: "mcp_unavailable", message: error?.message || "Trosa MCP unavailable" } }) }], details: {} }; }
    } });
}

const optionalLimit = Type.Optional(Type.Integer({ minimum: 1, maximum: 30 }));
const todayLimit = Type.Optional(Type.Integer({ minimum: 1, maximum: 50 }));
const tools = [
  bridge("search_customers", "Search customers", "Search Trosa customers via the MCP server.", Type.Object({ query: Type.String({ minLength: 1, maxLength: 200 }), limit: optionalLimit })),
  bridge("get_customer", "Get customer", "Get a customer ID returned by search_customers.", Type.Object({ customer_id: Type.Integer({ minimum: 1 }) })),
  bridge("get_today", "Get today", "Get all of the authenticated Hamid workspace's due or overdue Trosa tasks in one request.", Type.Object({ limit: todayLimit })),
  bridge("search_activity", "Search activity", "Search Trosa activity.", Type.Object({ customer_id: Type.Optional(Type.Integer({ minimum: 1 })), query: Type.Optional(Type.String({ maxLength: 200 })), limit: optionalLimit })),
  bridge("get_inbox", "Get inbox", "Get Trosa Inbox items.", Type.Object({ limit: optionalLimit })),
  bridge("get_contacts", "Get contacts", "Get contacts for a known customer.", Type.Object({ customer_id: Type.Integer({ minimum: 1 }) })),
  bridge("get_open_tasks", "Get open tasks", "Get open Trosa follow-up tasks.", Type.Object({ customer_id: Type.Optional(Type.Integer({ minimum: 1 })), limit: optionalLimit })),
  bridge("get_recent_actions", "Get recent actions", "Get recent Trosa Agent actions.", Type.Object({ limit: optionalLimit })),
  bridge("record_communication", "Record communication", "Record a confirmed customer communication via MCP.", Type.Object({ customer_id: Type.Integer({ minimum: 1 }), content: Type.String({ minLength: 1, maxLength: 4000 }), follow_date: Type.Optional(Type.String()), direction: Type.Optional(Type.String()), activity_type: Type.Optional(Type.String()), contact_id: Type.Optional(Type.Integer({ minimum: 1 })), source_reference: Type.Optional(Type.String()), next_task: Type.Optional(Type.String()), next_follow_up: Type.Optional(Type.String()), idempotency_key: Type.Optional(Type.String()) })),
  bridge("create_task", "Create task", "Create a dated customer task via MCP.", Type.Object({ customer_id: Type.Integer({ minimum: 1 }), title: Type.String({ minLength: 1, maxLength: 300 }), due_date: Type.String(), source_reference: Type.Optional(Type.String()), idempotency_key: Type.Optional(Type.String()) })),
  bridge("complete_task", "Complete task", "Complete a known task via MCP.", Type.Object({ task_id: Type.Integer({ minimum: 1 }), customer_id: Type.Optional(Type.Integer({ minimum: 1 })), completion_context: Type.Optional(Type.String()), idempotency_key: Type.Optional(Type.String()) })),
  bridge("undo_action", "Undo action", "Undo a returned Trosa action ID via MCP.", Type.Object({ action_id: Type.String({ minLength: 1 }), idempotency_key: Type.Optional(Type.String()) })),
  bridge("update_task", "Update task", "Update a known open task through MCP.", Type.Object({ task_id: Type.Integer({ minimum: 1 }), title: Type.Optional(Type.String()), content: Type.Optional(Type.String()), reason: Type.Optional(Type.String()), remind_date: Type.Optional(Type.String()), idempotency_key: Type.Optional(Type.String()) })),
  bridge("update_customer", "Update customer", "Update ordinary fields on a known customer.", Type.Object({ customer_id: Type.Integer({ minimum: 1 }), name: Type.Optional(Type.String()), company: Type.Optional(Type.String()), country: Type.Optional(Type.String()), website: Type.Optional(Type.String()), field: Type.Optional(Type.String()), industry: Type.Optional(Type.String()), profile: Type.Optional(Type.String()), notes: Type.Optional(Type.String()), tags: Type.Optional(Type.String()), idempotency_key: Type.Optional(Type.String()) })),
  bridge("update_contact", "Update contact", "Update ordinary fields on a known contact.", Type.Object({ contact_id: Type.Integer({ minimum: 1 }), name: Type.Optional(Type.String()), title: Type.Optional(Type.String()), email: Type.Optional(Type.String()), phone: Type.Optional(Type.String()), whatsapp: Type.Optional(Type.String()), linkedin: Type.Optional(Type.String()), preferred_channel: Type.Optional(Type.String()), contact_type: Type.Optional(Type.String()), notes: Type.Optional(Type.String()), idempotency_key: Type.Optional(Type.String()) })),
  bridge("resolve_inbox", "Resolve inbox", "Resolve a known Inbox item.", Type.Object({ inbox_item_id: Type.Integer({ minimum: 1 }), resolution_note: Type.Optional(Type.String()), idempotency_key: Type.Optional(Type.String()) })),
  bridge("assign_inbox_customer", "Assign Inbox customer", "Assign a known Inbox communication to a known customer.", Type.Object({ inbox_item_id: Type.Integer({ minimum: 1 }), customer_id: Type.Integer({ minimum: 1 }), idempotency_key: Type.Optional(Type.String()) })),
];

export default function (pi: ExtensionAPI) { for (const tool of tools) pi.registerTool(tool); }
