#!/usr/bin/env node
// Deterministic Hamid Today reader.  This intentionally skips the model and
// calls the same stdio MCP server used by the Pi extension.
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const transport = new StdioClientTransport({
  command: process.execPath,
  args: [new URL("./trosa-mcp-server.mjs", import.meta.url).pathname],
  env: {
    PATH: process.env.PATH || "",
    TROSA_GATEWAY_URL: process.env.TROSA_GATEWAY_URL || "",
    TROSA_GATEWAY_TOKEN: process.env.TROSA_GATEWAY_TOKEN || "",
    TROSA_GATEWAY_TIMEOUT_MS: process.env.TROSA_GATEWAY_TIMEOUT_MS || "",
  },
});

const client = new Client({ name: "trosa-hamid-today", version: "1.0.0" });
try {
  await client.connect(transport);
  const response = await client.callTool({ name: "get_today", arguments: { limit: 50 } });
  const block = response.content?.find((item) => item.type === "text");
  const payload = JSON.parse(block?.text || "{}");
  if (payload?.success === false) throw new Error(payload?.error?.message || "Trosa Gateway 暂时不可用");
  const tasks = payload?.data?.tasks || [];
  if (!tasks.length) {
    console.log("Hamid 今天没有到期的明确待办。");
  } else {
    console.log(`Hamid 今日待办：${tasks.length} 项（含逾期）`);
    for (const task of tasks) console.log(`- ${task.customer_name || "客户"}：${task.title || "待办"}（${task.due_date || "无日期"}）`);
  }
} catch (error) {
  console.error(`无法读取 Hamid 今日待办：${error?.message || "未知错误"}`);
  process.exitCode = 1;
} finally {
  await client.close();
}
