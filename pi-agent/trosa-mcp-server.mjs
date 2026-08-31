#!/usr/bin/env node

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { randomUUID } from "node:crypto";

const gatewayUrl = String(process.env.TROSA_GATEWAY_URL || "").replace(/\/$/, "");
const gatewayToken = String(process.env.TROSA_GATEWAY_TOKEN || "").trim();
const requestTimeoutMs = Math.max(5_000, Math.min(Number(process.env.TROSA_GATEWAY_TIMEOUT_MS || 45_000), 120_000));

function result(value, isError = false) {
  return { content: [{ type: "text", text: JSON.stringify(value) }], isError };
}

function error(code, message) {
  return result({ success: false, error: { code, message } }, true);
}

async function gateway(path, { method = "GET", body, idempotencyKey } = {}) {
  if (!gatewayUrl || !gatewayToken) return { status: 503, payload: { success: false, error: { code: "configuration", message: "Trosa Gateway is not configured" } } };
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), requestTimeoutMs);
  try {
    const headers = { Authorization: `Bearer ${gatewayToken}`, Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
    const response = await fetch(`${gatewayUrl}${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal });
    let payload;
    try { payload = await response.json(); } catch { payload = { success: false, error: { code: "invalid_response", message: "Trosa Gateway returned an invalid response" } }; }
    return { status: response.status, payload };
  } catch (cause) {
    return { status: 503, payload: { success: false, error: { code: "unavailable", message: cause?.name === "AbortError" ? "Trosa Gateway request timed out" : "Trosa Gateway is unavailable" } } };
  } finally {
    clearTimeout(timeout);
  }
}

async function callGateway(path, options) {
  const response = await gateway(path, options);
  return response.status >= 200 && response.status < 300 && response.payload?.success !== false
    ? result(response.payload)
    : result(response.payload || { success: false, error: { code: "internal_error", message: "Gateway request failed" } }, true);
}

function writeKey(value) {
  const key = String(value || "").trim();
  return key || `mcp:${randomUUID()}`;
}

const server = new McpServer({ name: "trosa-gateway", version: "1.0.0" });

server.registerTool("search_customers", { description: "Search the authenticated Trosa user's customers.", inputSchema: { query: z.string().min(1).max(200), limit: z.number().int().min(1).max(20).optional() } },
  ({ query, limit }) => callGateway(`/api/gateway/customers?query=${encodeURIComponent(query)}&limit=${limit || 10}`));
server.registerTool("get_customer", { description: "Get one customer previously returned by search_customers.", inputSchema: { customer_id: z.number().int().positive() } },
  ({ customer_id }) => callGateway(`/api/gateway/customers/${customer_id}`));
server.registerTool("get_today", { description: "Get explicit customer tasks due today.", inputSchema: { limit: z.number().int().min(1).max(30).optional() } },
  ({ limit }) => callGateway(`/api/gateway/today?limit=${limit || 15}`));
server.registerTool("search_activity", { description: "Search recent CRM activity without guessing identities.", inputSchema: { customer_id: z.number().int().positive().optional(), query: z.string().max(200).optional(), limit: z.number().int().min(1).max(30).optional() } },
  ({ customer_id, query, limit }) => { const params = new URLSearchParams({ limit: String(limit || 15) }); if (customer_id) params.set("customer_id", String(customer_id)); if (query) params.set("query", query); return callGateway(`/api/gateway/activity?${params}`); });
server.registerTool("get_inbox", { description: "Get the authenticated user's Trosa Inbox.", inputSchema: { limit: z.number().int().min(1).max(30).optional() } },
  ({ limit }) => callGateway(`/api/gateway/inbox?limit=${limit || 15}`));

const writeSchema = { idempotency_key: z.string().min(1).max(200).optional() };
server.registerTool("record_communication", { description: "Record a confirmed customer communication. Never guess customer or contact IDs.", inputSchema: { customer_id: z.number().int().positive(), content: z.string().min(1).max(4000), follow_date: z.string().optional(), direction: z.enum(["inbound", "outbound", "two_way", "unknown"]).optional(), activity_type: z.string().max(100).optional(), contact_id: z.number().int().positive().optional(), source_reference: z.string().max(300).optional(), next_task: z.string().max(300).optional(), next_follow_up: z.string().optional(), ...writeSchema } },
  (args) => callGateway("/api/gateway/actions", { method: "POST", idempotencyKey: writeKey(args.idempotency_key), body: { action: "record_communication", customer_id: args.customer_id, payload: { content: args.content, follow_date: args.follow_date || "", direction: args.direction || "unknown", activity_type: args.activity_type || "follow_up", contact_id: args.contact_id || null, source: "trosa_mcp", source_reference: args.source_reference || "", next_task: args.next_task || "", next_follow_up: args.next_follow_up || "" } } }));
server.registerTool("create_task", { description: "Create an explicit customer task with a date. Never invent the date.", inputSchema: { customer_id: z.number().int().positive(), title: z.string().min(1).max(300), due_date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/), source_reference: z.string().max(300).optional(), ...writeSchema } },
  (args) => callGateway("/api/gateway/actions", { method: "POST", idempotencyKey: writeKey(args.idempotency_key), body: { action: "create_task", customer_id: args.customer_id, payload: { title: args.title, due_date: args.due_date, source: "trosa_mcp", source_reference: args.source_reference || "" } } }));
server.registerTool("complete_task", { description: "Complete a task already read from Trosa. Do not guess task IDs.", inputSchema: { task_id: z.number().int().positive(), customer_id: z.number().int().positive().optional(), completion_context: z.string().max(1000).optional(), ...writeSchema } },
  (args) => callGateway("/api/gateway/actions", { method: "POST", idempotencyKey: writeKey(args.idempotency_key), body: { action: "complete_task", customer_id: args.customer_id || null, payload: { task_id: args.task_id, completion_context: args.completion_context || "", source: "trosa_mcp" } } }));
server.registerTool("undo_action", { description: "Undo an action_id returned by a prior Trosa write. It restores the complete shared action group.", inputSchema: { action_id: z.string().regex(/^agact_[A-Za-z0-9_-]{16,64}$/), idempotency_key: z.string().min(1).max(200).optional() } },
  ({ action_id, idempotency_key }) => callGateway(`/api/gateway/actions/${encodeURIComponent(action_id)}/undo`, { method: "POST", body: {}, idempotencyKey: writeKey(idempotency_key) }));

await server.connect(new StdioServerTransport());
