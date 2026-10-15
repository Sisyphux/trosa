#!/usr/bin/env node
/*
 * 「一笔」手绘图标生成器。
 *
 * 同一个确定性生成器画出全站图标：每个图标按名字做种子，抖动幅度、线宽、
 * 浅灰片偏移都一致，所以是一家人；新增图标只要在 ICONS 里加一行，重新运行即可。
 *
 *   node tools/hand_icons.mjs            # 写出 / 更新全部生成物
 *   node tools/hand_icons.mjs --check    # 只校验生成物是否最新（CI / 门禁用）
 *
 * 生成物：
 *   app/static/icons/hand/<name>[-20|-16].svg   CSS mask 用的静态图（24 / 20 / 16 三档线宽）
 *   app/static/hand-icons.js                    内联 SVG 用的路径数据 + handIcon()
 *   app/static/visual-v2.css   中间标记块       .ui-icon-<name> 的 mask 映射
 *   app/static/index.html      data-hand 占位   静态内联图标的路径
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const STATIC = path.join(ROOT, 'app/static');
const OUT_DIR = path.join(STATIC, 'icons/hand');

// 手绘程度：1 = 方案页的“轻微”，2 以上一眼能看出是手画的。16px 下仍须清楚可辨。
const HAND = 2.1;
// 浅灰片（低透明度墨色）相对线稿错开的距离，单位 viewBox。
const PATCH_DX = 1.5;
const PATCH_DY = 1.7;
const PATCH_OPACITY = 0.1;
// 线宽按显示尺寸分档（单位 viewBox，渲染后约 1.4–1.6px）。
const TIERS = [
  { suffix: '', px: 24, width: 1.6, patch: true },
  { suffix: '-20', px: 20, width: 1.85, patch: true },
  { suffix: '-16', px: 16, width: 2.2, patch: false },
];

function rng(seed) {
  let a = 7;
  for (const c of seed) a = (Math.imul(a, 31) + c.charCodeAt(0)) | 0;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const f = (n) => +n.toFixed(2);

class Pen {
  constructor(r, s) { this.r = r; this.s = s; this.d = ''; this.x = 0; this.y = 0; }
  j(v) { return v + (this.r() - 0.5) * 0.24 * this.s; }
  M(x, y) { x = this.j(x); y = this.j(y); this.d += `M${f(x)} ${f(y)}`; this.x = x; this.y = y; return this; }
  L(x, y) {
    x = this.j(x); y = this.j(y);
    const dx = x - this.x, dy = y - this.y, len = Math.hypot(dx, dy) || 1, nx = -dy / len, ny = dx / len;
    const amp = Math.min(0.5 * this.s, len * 0.045), sg = this.r() < 0.5 ? -1 : 1;
    const b1 = sg * amp * (0.5 + 0.5 * this.r()), b2 = sg * amp * (0.3 + 0.7 * this.r());
    this.d += `C${f(this.x + dx / 3 + nx * b1)} ${f(this.y + dy / 3 + ny * b1)} ${f(this.x + 2 * dx / 3 + nx * b2)} ${f(this.y + 2 * dy / 3 + ny * b2)} ${f(x)} ${f(y)}`;
    this.x = x; this.y = y; return this;
  }
  A(cx, cy, rx, ry, a0, a1) {
    const n = Math.max(1, Math.ceil(Math.abs(a1 - a0) / 90)), da = (a1 - a0) / n;
    let rj = 1 + (this.r() - 0.5) * 0.06 * this.s;
    for (let i = 0; i < n; i++) {
      const t0 = ((a0 + da * i) * Math.PI) / 180, t1 = ((a0 + da * (i + 1)) * Math.PI) / 180, k = (4 / 3) * Math.tan((t1 - t0) / 4);
      const r0 = rj; rj = 1 + (this.r() - 0.5) * 0.06 * this.s; const r1 = rj;
      const p1x = cx + rx * r1 * Math.cos(t1), p1y = cy + ry * r1 * Math.sin(t1);
      const c1x = this.x - k * rx * r0 * Math.sin(t0), c1y = this.y + k * ry * r0 * Math.cos(t0);
      const c2x = p1x + k * rx * r1 * Math.sin(t1), c2y = p1y - k * ry * r1 * Math.cos(t1);
      this.d += `C${f(c1x)} ${f(c1y)} ${f(c2x)} ${f(c2y)} ${f(p1x)} ${f(p1y)}`;
      this.x = p1x; this.y = p1y;
    }
    return this;
  }
}

class G {
  constructor(r, s) { this.r = r; this.sc = s; this.s = []; this.p = []; this.dots = []; }
  pen() { return new Pen(this.r, this.sc); }
  add(d, patch) { this.s.push(d); if (patch) this.p.push(d); }
  line(x1, y1, x2, y2) { this.add(this.pen().M(x1, y1).L(x2, y2).d); }
  poly(pts, closed, patch) {
    const p = this.pen().M(...pts[0]);
    pts.slice(1).forEach((q) => p.L(...q));
    if (closed) p.L(...pts[0]);
    this.add(p.d, patch);
  }
  rect(x, y, w, h, patch) { this.poly([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], true, patch); }
  ellipse(cx, cy, rx, ry, patch) {
    // 起笔点随机，收笔略过头：像一笔画圈没有严丝合缝。
    const a0 = 200 + this.r() * 60, t = (a0 * Math.PI) / 180;
    const p = this.pen().M(cx + rx * Math.cos(t), cy + ry * Math.sin(t)).A(cx, cy, rx, ry, a0, a0 + 360 + 8 * this.sc);
    this.add(p.d, patch);
  }
  circle(cx, cy, r, patch) { this.ellipse(cx, cy, r, r, patch); }
  arc(cx, cy, r, a0, a1, patch) {
    const t = (a0 * Math.PI) / 180;
    this.add(this.pen().M(cx + r * Math.cos(t), cy + r * Math.sin(t)).A(cx, cy, r, r, a0, a1).d, patch);
  }
  capsule(x1, y1, x2, y2, r) {
    const dx = x2 - x1, dy = y2 - y1, l = Math.hypot(dx, dy), nx = (-dy / l) * r, ny = (dx / l) * r;
    const th = (Math.atan2(ny, nx) * 180) / Math.PI;
    const p = this.pen().M(x1 + nx, y1 + ny).L(x2 + nx, y2 + ny).A(x2, y2, r, r, th, th - 180).L(x1 - nx, y1 - ny).A(x1, y1, r, r, th - 180, th - 360);
    this.add(p.d);
  }
  pin(cx, cy, r, tipY, patch) {
    const a = 145, b = 395, ta = (a * Math.PI) / 180;
    const p = this.pen().M(cx, tipY).L(cx + r * Math.cos(ta), cy + r * Math.sin(ta)).A(cx, cy, r, r, a, b).L(cx, tipY);
    this.add(p.d, patch);
  }
  dot(x, y, r) { this.dots.push([x + (this.r() - 0.5) * 0.2 * this.sc, y + (this.r() - 0.5) * 0.2 * this.sc, r || 1.25]); }
}

const star = () => {
  const p = [];
  for (let i = 0; i < 10; i++) {
    const a = ((-90 + i * 36) * Math.PI) / 180, r = i % 2 ? 3.8 : 8.6;
    p.push([12 + r * Math.cos(a), 12.8 + r * Math.sin(a)]);
  }
  return p;
};

/* name: [旧 Phosphor 文件（仅对照）, 中文用途, 画法]；patch 只给闭合的主体形 */
const ICONS = {
  clock: ['clock', '时间', (g) => { g.circle(12, 12, 8.6, 1); g.poly([[12, 7.4], [12, 12.3], [15.3, 14.3]]); }],
  users: ['users', '客户', (g) => { g.circle(9.4, 7.6, 3.1, 1); g.arc(9.4, 20, 5.9, 180, 360, 1); g.arc(15.6, 7.6, 2.6, -90, 90); g.arc(15.2, 20, 5.3, -62, 0); }],
  message: ['chat-circle-text', '对话 / 记录沟通', (g) => { g.poly([[4, 5.2], [20, 5.2], [20, 15.4], [12.6, 15.4], [8.2, 19.4], [8.2, 15.4], [4, 15.4]], 1, 1); g.line(8, 9.2, 16, 9.2); g.line(8, 12.1, 13, 12.1); }],
  calendar: ['calendar-blank', '日历', (g) => { g.rect(4, 5.6, 16, 14.6, 1); g.line(8.5, 3.4, 8.5, 7.6); g.line(15.5, 3.4, 15.5, 7.6); g.line(4, 10.2, 20, 10.2); g.dot(9, 14.8); g.dot(12.4, 14.8); }],
  search: ['magnifying-glass', '搜索', (g) => { g.circle(10.6, 10.6, 6.3, 1); g.line(15.3, 15.4, 20, 20.1); }],
  settings: ['gear-six', '设置', (g) => { g.line(4, 7.2, 20, 7.2); g.line(4, 12, 20, 12); g.line(4, 16.8, 20, 16.8); g.circle(9.2, 7.2, 2.2, 1); g.circle(15, 12, 2.2, 1); g.circle(8.2, 16.8, 2.2, 1); }],
  done: ['check', '完成（新增）', (g) => { g.circle(12, 12, 8.6, 1); g.poly([[7.8, 12.4], [10.8, 15.3], [16.3, 9.2]]); }],
  plus: ['plus', '添加', (g) => { g.line(12, 5, 12, 19); g.line(5, 12, 19, 12); }],
  edit: ['pencil-simple', '编辑', (g) => { g.poly([[4.6, 19.4], [5.5, 15.3], [16.2, 4.6], [19.4, 7.8], [8.7, 18.5]], 1, 1); g.line(14.2, 6.6, 17.4, 9.8); }],
  check: ['check', '勾', (g) => { g.poly([[4.8, 12.6], [9.8, 17.4], [19.2, 7]]); }],
  close: ['x', '关闭', (g) => { g.line(6, 6, 18, 18); g.line(18, 6, 6, 18); }],
  alert: ['warning', '提醒', (g) => { g.poly([[12, 3.8], [21, 19.6], [3, 19.6]], 1, 1); g.line(12, 9.4, 12, 14); g.dot(12, 16.9); }],
  info: ['info', '说明', (g) => { g.circle(12, 12, 8.6, 1); g.line(12, 11, 12, 16.4); g.dot(12, 7.8); }],
  sparkle: ['sparkle', 'AI 建议', (g) => { g.poly([[12, 3], [13.9, 10.1], [21, 12], [13.9, 13.9], [12, 21], [10.1, 13.9], [3, 12], [10.1, 10.1]], 1, 1); }],
  list: ['list-bullets', '列表 / 日志', (g) => { g.line(9, 7, 20, 7); g.line(9, 12, 20, 12); g.line(9, 17, 16, 17); g.dot(4.8, 7); g.dot(4.8, 12); g.dot(4.8, 17); }],
  mail: ['envelope-simple', '邮箱', (g) => { g.rect(3.5, 6, 17, 12.5, 1); g.poly([[4.2, 7], [12, 13], [19.8, 7]]); }],
  file: ['paperclip', '文件', (g) => { g.poly([[6, 3.5], [14, 3.5], [18.5, 8], [18.5, 20.5], [6, 20.5]], 1, 1); g.poly([[14, 3.5], [14, 8], [18.5, 8]]); }],
  archive: ['archive', '归档', (g) => { g.rect(3.5, 4, 17, 5, 1); g.poly([[5, 9], [5, 20], [19, 20], [19, 9]]); g.line(10, 13, 14, 13); }],
  trash: ['trash', '删除', (g) => { g.line(4, 7, 20, 7); g.poly([[9.5, 7], [9.8, 4.5], [14.2, 4.5], [14.5, 7]]); g.poly([[6, 7], [7, 20], [17, 20], [18, 7]]); g.line(10, 11, 10, 16); g.line(14, 11, 14, 16); }],
  export: ['download-simple', '导出', (g) => { g.line(12, 4, 12, 15); g.poly([[7.5, 10.5], [12, 15], [16.5, 10.5]]); g.line(4.5, 20, 19.5, 20); }],
  refresh: ['arrows-clockwise', '重新加载', (g) => { g.arc(12, 12, 7.4, 200, 335); g.poly([[15.8, 9.4], [18.7, 8.9], [19.2, 5.8]]); g.arc(12, 12, 7.4, 20, 155); g.poly([[8.2, 14.6], [5.3, 15.1], [4.8, 18.2]]); }],
  external: ['arrow-square-out', '在新窗口打开', (g) => { g.poly([[13.5, 4], [20, 4], [20, 10.5]]); g.line(20, 4, 11, 13); g.poly([[17, 13.5], [17, 20], [4, 20], [4, 7], [10.5, 7]]); }],
  open: ['identification-card', '查看原文 / 资料', (g) => { g.rect(3, 5, 18, 14, 1); g.circle(8.6, 10.6, 2.1); g.arc(8.6, 17.6, 3.4, 200, 340); g.line(13.6, 10, 18, 10); g.line(13.6, 13.8, 16.8, 13.8); }],
  link: ['link-simple', '复制链接', (g) => { g.capsule(6.2, 17.8, 10.4, 13.6, 2.9); g.capsule(13.6, 10.4, 17.8, 6.2, 2.9); g.line(9.6, 14.4, 14.4, 9.6); }],
  globe: ['globe', '官网', (g) => { g.circle(12, 12, 8.6, 1); g.ellipse(12, 12, 3.6, 8.6); g.line(3.6, 12, 20.4, 12); }],
  map: ['map-pin', '地址', (g) => { g.pin(12, 10, 6, 20.6, 1); g.circle(12, 10, 2.2); }],
  star: ['star', '重点', (g) => { g.poly(star(), 1, 1); }],
  more: ['dots-three', '更多', (g) => { g.dot(6, 12, 1.5); g.dot(12, 12, 1.5); g.dot(18, 12, 1.5); }],
  'sign-out': ['sign-out', '切换账号', (g) => { g.poly([[10, 4], [4.6, 4], [4.6, 20], [10, 20]]); g.line(9.6, 12, 20, 12); g.poly([[16, 8], [20, 12], [16, 16]]); }],
  left: ['caret-left', '上一页', (g) => { g.poly([[15, 5], [8, 12], [15, 19]]); }],
  right: ['caret-right', '下一页', (g) => { g.poly([[9, 5], [16, 12], [9, 19]]); }],
  up: ['caret-up', '收起', (g) => { g.poly([[5, 15.5], [12, 8.5], [19, 15.5]]); }],
  down: ['caret-down', '展开', (g) => { g.poly([[5, 8.5], [12, 15.5], [19, 8.5]]); }],
  today: ['clock', '今天（新增）', (g) => { g.arc(12, 15, 7, 180, 360, 1); g.line(3, 15, 21, 15); g.line(12, 3.6, 12, 5.4); g.line(5.2, 6.6, 6.4, 7.8); g.line(18.8, 6.6, 17.6, 7.8); g.line(7, 19.2, 17, 19.2); }],
  inbox: ['', 'Inbox（新增）', (g) => { g.poly([[3.5, 13], [6.6, 5], [17.4, 5], [20.5, 13], [20.5, 19.5], [3.5, 19.5]], 1, 1); g.poly([[3.5, 13], [8.4, 13], [9.6, 15.6], [14.4, 15.6], [15.6, 13], [20.5, 13]]); }],
  tidy: ['', '整理逾期（新增）', (g) => { g.line(4.5, 6.5, 15, 6.5); g.line(4.5, 12, 15, 12); g.line(4.5, 17.5, 11, 17.5); g.poly([[14.2, 16.8], [16.6, 19.2], [20.6, 14.6]]); }],
  wait: ['', '等待回复（新增）', (g) => { g.poly([[7, 4], [17, 4], [17, 7.6], [12, 12], [17, 16.4], [17, 20], [7, 20], [7, 16.4], [12, 12], [7, 7.6]], 1, 1); g.line(5, 4, 19, 4); g.line(5, 20, 19, 20); }],
  none: ['', '尚无 / 未安排（新增）', (g) => { for (let i = 0; i < 6; i++) g.arc(12, 12, 8.2, i * 60 + 10, i * 60 + 42); }],
  batch: ['', '批量添加（新增）', (g) => { g.rect(3.5, 3.5, 7, 7, 1); g.rect(13.5, 3.5, 7, 7); g.rect(3.5, 13.5, 7, 7); g.line(17, 13.4, 17, 20.6); g.line(13.4, 17, 20.6, 17); }],
};

/* ---- 空状态插画（二期）：96×72，同一个生成器，手绘程度更大（一张纸 + 一支笔） ---- */
const ILLUS_HAND = 4.2;
const QUILL = (x, y, k) => `<g class="q" transform="translate(${x} ${y}) scale(${k}) rotate(-8)"><path class="leaf" d="M0 0c7-1 15-7 24-14 7-5 15-4 15 2 0 9-11 17-24 17-6 0-10-2-15-5z"/><path class="sh" d="M1 -1L34 -13"/></g>`;
const DASH = '<path class="dash" d="M14 69c20-1 44 1 68 0"/>';
/* kind: [中文用途, 画法, 附加（笔 / 虚线）] */
const ILLUS = {
  inbox: ['Inbox 对话', (g) => { g.poly([[14, 10], [74, 10], [74, 46], [46, 46], [30, 60], [30, 46], [14, 46]], 1, 1); g.line(26, 24, 60, 24); g.line(26, 34, 48, 34); }, QUILL(66, 62, 0.62)],
  timeline: ['客户最近沟通', (g) => { g.rect(26, 6, 44, 60, 1); g.line(34, 18, 58, 18); g.line(34, 28, 58, 28); g.line(34, 38, 48, 38); }, QUILL(62, 58, 0.62)],
  todo: ['待办', (g) => { g.rect(20, 12, 52, 50, 1); g.line(32, 6, 32, 18); g.line(60, 6, 60, 18); g.line(20, 26, 72, 26); g.poly([[36, 44], [44, 51], [58, 36]]); }, ''],
  search: ['搜索无结果', (g) => { g.circle(40, 32, 17, 1); g.line(52.5, 44.5, 66, 58); }, DASH],
  customers: ['没有客户', (g) => { g.rect(14, 18, 54, 38, 1); g.circle(30, 33, 6); g.arc(30, 52, 10, 205, 335); g.line(44, 30, 58, 30); g.line(44, 40, 54, 40); g.line(72, 14, 72, 26); g.line(66, 20, 78, 20); }, ''],
  week: ['本周没有已确认的沟通', (g) => { g.rect(10, 16, 76, 42, 1); g.line(10, 28, 86, 28); g.line(35, 28, 35, 58); g.line(60, 28, 60, 58); g.dot(22, 43); g.dot(47, 43); g.dot(72, 43); }, QUILL(70, 64, 0.55)],
  contacts: ['联系人', (g) => { g.circle(48, 24, 12, 1); g.arc(48, 66, 24, 195, 345, 1); g.line(16, 66, 80, 66); }, ''],
  files: ['客户文件', (g) => { g.poly([[26, 8], [52, 8], [66, 22], [66, 62], [26, 62]], 1, 1); g.poly([[52, 8], [52, 22], [66, 22]]); g.line(34, 36, 58, 36); g.line(34, 46, 52, 46); }, ''],
  day: ['当天没有安排', (g) => { g.rect(18, 14, 60, 48, 1); g.line(34, 8, 34, 20); g.line(62, 8, 62, 20); g.line(18, 28, 78, 28); g.line(36, 46, 60, 46); }, ''],
  log: ['记录 / 日志为空', (g) => { g.rect(24, 6, 48, 60, 1); g.dot(34, 20, 1.6); g.dot(34, 32, 1.6); g.dot(34, 44, 1.6); g.line(42, 20, 62, 20); g.line(42, 32, 62, 32); g.line(42, 44, 54, 44); }, ''],
  clear: ['今天没有要处理的', (g) => { g.circle(48, 32, 20, 1); g.poly([[38, 33], [45, 40], [59, 24]]); }, DASH],
};

function buildIllus(kind) {
  const [, draw, extra] = ILLUS[kind];
  const g = new G(rng('illus-' + kind), ILLUS_HAND);
  draw(g);
  const pt = g.p.length ? `<g class="pt" transform="translate(2.4 2.6)">${g.p.map((d) => `<path d="${d}"/>`).join('')}</g>` : '';
  const body = g.s.map((d) => `<path class="s" pathLength="1" d="${d}"/>`).join('');
  const dots = g.dots.map(([x, y, r]) => `<circle class="d" cx="${f(x)}" cy="${f(y)}" r="${r}"/>`).join('');
  return pt + body + dots + extra;
}

function build(name) {
  const g = new G(rng(name), HAND);
  ICONS[name][2](g);
  return {
    strokes: g.s,
    patches: g.p,
    dots: g.dots.map(([x, y, r]) => [f(x), f(y), r]),
  };
}

function maskSvg(icon, tier) {
  const patch = tier.patch && icon.patches.length
    ? `<g transform="translate(${PATCH_DX} ${PATCH_DY})" fill="#000" fill-opacity="${PATCH_OPACITY}" stroke="none">${icon.patches.map((d) => `<path d="${d}"/>`).join('')}</g>`
    : '';
  const body = icon.strokes.map((d) => `<path d="${d}"/>`).join('');
  const dots = icon.dots.map(([x, y, r]) => `<circle cx="${x}" cy="${y}" r="${r}" fill="#000" stroke="none"/>`).join('');
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#000" stroke-width="${tier.width}" stroke-linecap="round" stroke-linejoin="round">${patch}${body}${dots}</svg>\n`;
}

/* 内联 SVG 的内部结构：浅灰片 .pt（仅 .hi-id 显示）、描线 .s（pathLength=1，供悬停描线）、点 .d */
function inlineInner(icon) {
  const patch = icon.patches.length
    ? `<g class="pt" transform="translate(${PATCH_DX} ${PATCH_DY})">${icon.patches.map((d) => `<path d="${d}"/>`).join('')}</g>`
    : '';
  const body = icon.strokes.map((d) => `<path class="s" pathLength="1" d="${d}"/>`).join('');
  const dots = icon.dots.map(([x, y, r]) => `<circle class="d" cx="${x}" cy="${y}" r="${r}"/>`).join('');
  return patch + body + dots;
}

function cssBlock(names) {
  const lines = [
    '/* hand-icons:begin — 由 tools/hand_icons.mjs 生成，勿手改。默认 16px 档（只有线）；.ui-icon-md = 20px 档；.ui-icon-id = 24px 档，带浅灰片，只给身份位置用。 */',
  ];
  for (const n of names) {
    lines.push(`.ui-icon-${n} { --ui-icon: url("icons/hand/${n}-16.svg"); --ui-icon-md: url("icons/hand/${n}-20.svg"); --ui-icon-id: url("icons/hand/${n}.svg"); }`);
  }
  lines.push('.ui-icon.ui-icon-md { --ui-icon: var(--ui-icon-md); }');
  lines.push('.ui-icon.ui-icon-id { --ui-icon: var(--ui-icon-id); }');
  lines.push('/* hand-icons:end */');
  return lines.join('\n');
}

function jsModule(names, built) {
  const data = {};
  for (const n of names) data[n] = { i: inlineInner(built[n]) };
  const json = JSON.stringify(data, null, 0).replace(/\},"/g, '},\n"');
  const illus = {};
  for (const k of Object.keys(ILLUS)) illus[k] = buildIllus(k);
  const illusJson = JSON.stringify(illus, null, 0).replace(/","/g, '",\n"');
  return `/* 由 tools/hand_icons.mjs 生成，勿手改。「一笔」手绘图标的内联 SVG 内部结构。 */
(function(root) {
  'use strict';
  var HAND_ICONS = ${json};
  // handIcon('done', { cls: 'hi-id', size: 20, label: '已完成' }) -> 内联 SVG 字符串。
  // 默认只有线；cls 含 hi-id 时显示浅灰片（身份位置专用）。无 label 时对读屏隐藏。
  function handIcon(name, opts) {
    var d = HAND_ICONS[name];
    if (!d) return '';
    opts = opts || {};
    var cls = 'hi' + (opts.cls ? ' ' + opts.cls : '');
    var size = opts.size ? ' width="' + opts.size + '" height="' + opts.size + '"' : '';
    var a11y = opts.label ? ' role="img" aria-label="' + String(opts.label).replace(/"/g, '&quot;') + '"' : ' aria-hidden="true"';
    return '<svg class="' + cls + '" viewBox="0 0 24 24"' + size + a11y + ' focusable="false">' + d.i + '</svg>';
  }
  // 空状态插画（96×72）。handIllus('timeline', { cls: 'hi-draw' }) -> 内联 SVG；
  // hi-draw 只在该插画第一次出现时给（描一遍，之后静止）。装饰性：对读屏隐藏，文字由标题与说明承担。
  var HAND_ILLUS = ${illusJson};
  function handIllus(kind, opts) {
    var d = HAND_ILLUS[kind] || HAND_ILLUS.log;
    opts = opts || {};
    return '<svg class="hi hi-illus' + (opts.cls ? ' ' + opts.cls : '') + '" viewBox="0 0 96 72" aria-hidden="true" focusable="false">' + d + '</svg>';
  }
  root.HAND_ICONS = HAND_ICONS;
  root.HAND_ILLUS = HAND_ILLUS;
  root.handIcon = handIcon;
  root.handIllus = handIllus;
})(window);
`;
}

function fillIndexHtml(html, built) {
  // <svg class="hi ..." data-hand="name" ...>…</svg> —— 内部由生成器填充。
  const re = /<svg((?:(?!>)[^])*?)data-hand="([a-z-]+)"((?:(?!>)[^])*)>([^]*?)<\/svg>/g;
  return html.replace(re, (all, pre, name, post) => {
    if (!built[name]) throw new Error(`index.html 引用了不存在的手绘图标：${name}`);
    return `<svg${pre}data-hand="${name}"${post}>${inlineInner(built[name])}</svg>`;
  });
}

function main() {
  const check = process.argv.includes('--check');
  const names = Object.keys(ICONS);
  const built = Object.fromEntries(names.map((n) => [n, build(n)]));
  const outputs = new Map();

  for (const n of names) for (const t of TIERS) outputs.set(path.join(OUT_DIR, `${n}${t.suffix}.svg`), maskSvg(built[n], t));
  outputs.set(path.join(STATIC, 'hand-icons.js'), jsModule(names, built));

  const cssPath = path.join(STATIC, 'visual-v2.css');
  const css = fs.readFileSync(cssPath, 'utf8');
  const m = /\/\* hand-icons:begin[^]*?\/\* hand-icons:end \*\//.exec(css);
  if (!m) throw new Error('visual-v2.css 缺少 hand-icons:begin/end 标记块');
  outputs.set(cssPath, css.replace(m[0], cssBlock(names)));

  const htmlPath = path.join(STATIC, 'index.html');
  outputs.set(htmlPath, fillIndexHtml(fs.readFileSync(htmlPath, 'utf8'), built));

  let stale = 0;
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const expected = new Set([...outputs.keys()].map((p) => path.resolve(p)));
  for (const [file, content] of outputs) {
    const current = fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : null;
    if (current === content) continue;
    stale++;
    if (check) console.error(`过期：${path.relative(ROOT, file)}`);
    else fs.writeFileSync(file, content);
  }
  // 清理已从 ICONS 中移除的图标文件
  for (const file of fs.readdirSync(OUT_DIR)) {
    const full = path.resolve(OUT_DIR, file);
    if (!expected.has(full) && file.endsWith('.svg')) {
      stale++;
      if (check) console.error(`多余：${path.relative(ROOT, full)}`);
      else fs.unlinkSync(full);
    }
  }
  if (check) {
    if (stale) { console.error(`手绘图标生成物与生成器不一致（${stale} 处），请运行 node tools/hand_icons.mjs`); process.exit(1); }
    console.log(`手绘图标生成物是最新的（${names.length} 个图标）。`);
  } else {
    console.log(`已生成 ${names.length} 个图标，更新 ${stale} 个文件。`);
  }
}

main();
