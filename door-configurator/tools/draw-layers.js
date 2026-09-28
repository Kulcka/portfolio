#!/usr/bin/env node
/*
 * Рисует демонстрационные слои двери (SVG) и миниатюры вариантов.
 *
 *   node tools/draw-layers.js
 *
 * Результат: layers/**.svg (холст 600 × 1000, прозрачный фон) и thumbs/**.svg (60 × 90).
 * Нужен только для демо: на сайте заказчика эти файлы заменяются его PNG,
 * а пути к ним прописываются в config.js. Скрипт детерминирован — повторный запуск
 * даёт те же файлы.
 */
'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const W = 600;
const H = 1000;

// ---------- геометрия сцены (px холста) ----------
const FLOOR = 930;
const FRAME = 16; // ширина профиля короба
const LEAF_TOP = 290;
const LEAF_H = FLOOR - LEAF_TOP; // 640
const MAIN_W = 260;
const NARROW_W = 110;
const GAP = 4;
const SIDE_W = 72;
const TRANSOM_H = 110;
const HANDLE_Y = 618; // ~1000 мм от пола
const PEEPHOLE_Y = 462; // ~1500 мм от пола

function geometry(size) {
  const inner = size === 'single' ? MAIN_W : MAIN_W + GAP + NARROW_W;
  const outerL = Math.round(W / 2 - inner / 2 - FRAME);
  const innerL = outerL + FRAME;
  const main = { x: innerL, y: LEAF_TOP, w: MAIN_W, h: LEAF_H };
  const narrow = size === 'single' ? null : { x: innerL + MAIN_W + GAP, y: LEAF_TOP, w: NARROW_W, h: LEAF_H };
  return {
    main,
    narrow,
    leaves: narrow ? [main, narrow] : [main],
    outerL,
    outerR: innerL + inner + FRAME,
    innerL,
    innerR: innerL + inner,
    top: LEAF_TOP - FRAME,
    handleX: main.x + main.w - 30,
  };
}

// ---------- утилиты ----------
function rng(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const r1 = (n) => Math.round(n * 10) / 10;

function svgDoc(body, { width = W, height = H, viewBox } = {}) {
  const vb = viewBox || `0 0 ${width} ${height}`;
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="${vb}">\n` +
    body.trim() +
    '\n</svg>\n'
  );
}

function write(rel, content) {
  const file = path.join(ROOT, rel);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, content.replace(/\r\n/g, '\n'), 'utf8');
}

function rect(r, attrs = '') {
  return `<rect x="${r1(r.x)}" y="${r1(r.y)}" width="${r1(r.w)}" height="${r1(r.h)}" ${attrs}/>`;
}

function bbox(rects) {
  const x = Math.min(...rects.map((r) => r.x));
  const y = Math.min(...rects.map((r) => r.y));
  const x2 = Math.max(...rects.map((r) => r.x + r.w));
  const y2 = Math.max(...rects.map((r) => r.y + r.h));
  return { x, y, w: x2 - x, h: y2 - y };
}

// ---------- материалы полотна ----------
function woodGrain(box, seed, palette) {
  const rand = rng(seed);
  let out = '';
  // широкие мягкие полосы — неоднородность тона
  for (let i = 0; i < box.w / 14; i++) {
    const x = box.x + rand() * box.w;
    const c = palette.bands[Math.floor(rand() * palette.bands.length)];
    out += `<rect x="${r1(x)}" y="${box.y}" width="${r1(4 + rand() * 12)}" height="${box.h}" fill="${c}" opacity="${r1(0.08 + rand() * 0.1)}"/>`;
  }
  // волокна
  const count = Math.round(box.w / 3.2);
  for (let i = 0; i < count; i++) {
    let x = box.x + (i / count) * box.w + rand() * 3;
    const phase = rand() * Math.PI * 2;
    const amp = 0.6 + rand() * 1.6;
    let d = `M${r1(x)} ${box.y - 5}`;
    for (let y = box.y + 30; y <= box.y + box.h + 30; y += 30) {
      x += (rand() - 0.5) * 0.9;
      d += ` L${r1(x + Math.sin(y / 70 + phase) * amp)} ${y}`;
    }
    const c = palette.lines[Math.floor(rand() * palette.lines.length)];
    out += `<path d="${d}" stroke="${c}" stroke-width="${r1(0.4 + rand() * 1.4)}" opacity="${r1(0.25 + rand() * 0.45)}" fill="none"/>`;
  }
  return out;
}

function noiseFilter(id, freq, octaves, alphaMax, seed = 3) {
  return (
    `<filter id="${id}" x="0" y="0" width="1" height="1">` +
    `<feTurbulence type="fractalNoise" baseFrequency="${freq}" numOctaves="${octaves}" seed="${seed}"/>` +
    `<feColorMatrix type="saturate" values="0"/>` +
    `<feComponentTransfer><feFuncA type="table" tableValues="0 ${alphaMax}"/></feComponentTransfer>` +
    `<feComposite operator="in" in2="SourceGraphic"/></filter>`
  );
}

/** Возвращает { defs, body } — заливку материала внутри прямоугольника box. */
function material(kind, box, seed = 11) {
  const b = rect(box);
  switch (kind) {
    case 'graphite':
      return {
        defs:
          '<linearGradient id="mg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#4a4e54"/><stop offset="1" stop-color="#383b40"/></linearGradient>' +
          noiseFilter('mn', 0.85, 2, 0.28),
        body: b.replace('/>', 'fill="url(#mg)"/>') + b.replace('/>', 'fill="#fff" filter="url(#mn)"/>'),
      };
    case 'copper':
      return {
        defs:
          '<linearGradient id="mg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#8a5a33"/><stop offset=".5" stop-color="#a86d3c"/><stop offset="1" stop-color="#6f4628"/></linearGradient>' +
          '<filter id="mp" x="0" y="0" width="1" height="1"><feTurbulence type="fractalNoise" baseFrequency="0.018 0.03" numOctaves="4" seed="7"/>' +
          '<feColorMatrix type="matrix" values="0 0 0 0 0.10  0 0 0 0 0.06  0 0 0 0 0.03  2.6 0 0 0 -1.05"/>' +
          '<feComposite operator="in" in2="SourceGraphic"/></filter>' +
          noiseFilter('mn', 0.9, 2, 0.22),
        body:
          b.replace('/>', 'fill="url(#mg)"/>') +
          b.replace('/>', 'fill="#000" filter="url(#mp)"/>') +
          b.replace('/>', 'fill="#fff" filter="url(#mn)"/>'),
      };
    case 'wenge':
      return {
        defs: '',
        body:
          b.replace('/>', 'fill="#3e2b21"/>') +
          woodGrain(box, seed, { lines: ['#1f140f', '#2a1b14', '#5a4133', '#4a3428'], bands: ['#24170f', '#56402f'] }),
      };
    case 'oak':
      return {
        defs: '',
        body:
          b.replace('/>', 'fill="#dccfb6"/>') +
          woodGrain(box, seed + 1, { lines: ['#b9a684', '#c7b594', '#a8936f', '#efe5d2'], bands: ['#cbbb9b', '#eae0cc'] }),
      };
    case 'concrete': {
      const rand = rng(seed + 2);
      let pores = '';
      const n = Math.round((box.w * box.h) / 900);
      for (let i = 0; i < n; i++) {
        pores += `<circle cx="${r1(box.x + rand() * box.w)}" cy="${r1(box.y + rand() * box.h)}" r="${r1(0.5 + rand() * 1.3)}" fill="#5f5c58" opacity="${r1(0.25 + rand() * 0.4)}"/>`;
      }
      return {
        defs:
          '<filter id="mc" x="0" y="0" width="1" height="1"><feTurbulence type="fractalNoise" baseFrequency="0.011" numOctaves="5" seed="4"/>' +
          '<feColorMatrix type="matrix" values="0 0 0 0 1  0 0 0 0 1  0 0 0 0 1  0 0 0 1.4 -0.55"/>' +
          '<feComposite operator="in" in2="SourceGraphic"/></filter>' +
          '<filter id="md" x="0" y="0" width="1" height="1"><feTurbulence type="fractalNoise" baseFrequency="0.035" numOctaves="3" seed="9"/>' +
          '<feColorMatrix type="matrix" values="0 0 0 0 0.2  0 0 0 0 0.2  0 0 0 0 0.19  1.6 0 0 0 -0.78"/>' +
          '<feComposite operator="in" in2="SourceGraphic"/></filter>' +
          noiseFilter('mn', 0.9, 2, 0.2),
        body:
          b.replace('/>', 'fill="#a19e99"/>') +
          b.replace('/>', 'fill="#fff" filter="url(#mc)" opacity=".55"/>') +
          b.replace('/>', 'fill="#000" filter="url(#md)" opacity=".5"/>') +
          b.replace('/>', 'fill="#fff" filter="url(#mn)"/>') +
          pores,
      };
    }
    case 'white':
      return {
        defs: '<linearGradient id="mg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#f3f2ee"/><stop offset="1" stop-color="#e3e1db"/></linearGradient>',
        body: b.replace('/>', 'fill="url(#mg)"/>'),
      };
    // цвета короба
    case 'frame-black':
      return {
        defs:
          '<linearGradient id="mg" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#2a2b2e"/><stop offset="1" stop-color="#1b1c1e"/></linearGradient>' +
          noiseFilter('mn', 1.1, 2, 0.3),
        body: b.replace('/>', 'fill="url(#mg)"/>') + b.replace('/>', 'fill="#fff" filter="url(#mn)" opacity=".5"/>'),
      };
    case 'frame-graphite':
      return {
        defs:
          '<linearGradient id="mg" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#5a5e64"/><stop offset="1" stop-color="#474a4f"/></linearGradient>' +
          noiseFilter('mn', 0.9, 2, 0.2),
        body: b.replace('/>', 'fill="url(#mg)"/>') + b.replace('/>', 'fill="#fff" filter="url(#mn)" opacity=".5"/>'),
      };
    case 'frame-bronze':
      return {
        defs:
          '<linearGradient id="mg" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#7d6443"/><stop offset=".5" stop-color="#94784f"/><stop offset="1" stop-color="#5e4a31"/></linearGradient>' +
          noiseFilter('mn', 0.9, 2, 0.22),
        body: b.replace('/>', 'fill="url(#mg)"/>') + b.replace('/>', 'fill="#fff" filter="url(#mn)" opacity=".5"/>'),
      };
    default:
      throw new Error(`Неизвестный материал: ${kind}`);
  }
}

// ---------- слои ----------
function sceneLayer() {
  const rand = rng(5);
  let joints = '';
  for (let i = -6; i <= 12; i++) {
    const xb = i * 100;
    const xt = W / 2 + (xb - W / 2) * 0.72;
    joints += `<line x1="${r1(xt)}" y1="${FLOOR}" x2="${r1(xb)}" y2="${H}" stroke="#8f887e" stroke-opacity=".35" stroke-width="1"/>`;
  }
  joints += `<line x1="0" y1="968" x2="${W}" y2="968" stroke="#8f887e" stroke-opacity=".3"/>`;
  let specks = '';
  for (let i = 0; i < 160; i++) {
    specks += `<circle cx="${r1(rand() * W)}" cy="${r1(rand() * 900)}" r="${r1(0.4 + rand() * 0.8)}" fill="#b9b3aa" opacity=".35"/>`;
  }
  return svgDoc(`
<defs>
<linearGradient id="wall" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#eeece8"/><stop offset="1" stop-color="#dfdcd6"/></linearGradient>
<radialGradient id="light" cx=".5" cy=".25" r=".7"><stop offset="0" stop-color="#fff" stop-opacity=".55"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></radialGradient>
<linearGradient id="floor" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#c4bdb3"/><stop offset="1" stop-color="#aea69b"/></linearGradient>
<linearGradient id="plinth" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#f7f6f3"/><stop offset="1" stop-color="#e2dfd9"/></linearGradient>
</defs>
<rect width="${W}" height="${FLOOR}" fill="url(#wall)"/>
<rect width="${W}" height="${FLOOR}" fill="url(#light)"/>
${specks}
<rect y="${FLOOR - 22}" width="${W}" height="22" fill="url(#plinth)"/>
<line x1="0" y1="${FLOOR - 22}" x2="${W}" y2="${FLOOR - 22}" stroke="#c9c5be"/>
<rect y="${FLOOR}" width="${W}" height="${H - FLOOR}" fill="url(#floor)"/>
${joints}
<rect y="${FLOOR}" width="${W}" height="3" fill="#000" opacity=".12"/>
`);
}

function sizeLayer(size) {
  const g = geometry(size);
  const w = g.outerR - g.outerL;
  return svgDoc(`
<defs>
<filter id="blur" x="0" y="0" width="${W}" height="${H}" filterUnits="userSpaceOnUse"><feGaussianBlur stdDeviation="7"/></filter>
<linearGradient id="thr" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#d9dcdf"/><stop offset=".5" stop-color="#9ea3a8"/><stop offset="1" stop-color="#6f7479"/></linearGradient>
</defs>
<ellipse cx="${W / 2}" cy="${FLOOR + 8}" rx="${w / 2 + 26}" ry="10" fill="#000" opacity=".28" filter="url(#blur)"/>
<rect x="${g.outerL - 4}" y="${FLOOR - 2}" width="${w + 8}" height="9" rx="2" fill="url(#thr)"/>
`);
}

function frameLayer(size, color) {
  const g = geometry(size);
  const box = { x: g.outerL, y: g.top, w: g.outerR - g.outerL, h: FLOOR - g.top };
  const m = material(`frame-${color}`, box);
  const hole = `M${g.innerL} ${LEAF_TOP} H${g.innerR} V${FLOOR} H${g.innerL} Z`;
  const outer = `M${g.outerL} ${g.top} H${g.outerR} V${FLOOR} H${g.outerL} Z`;
  return svgDoc(`
<defs>
${m.defs}
<clipPath id="bars"><path d="${outer} ${hole}" clip-rule="evenodd"/></clipPath>
<filter id="sh" x="0" y="0" width="${W}" height="${H}" filterUnits="userSpaceOnUse"><feGaussianBlur stdDeviation="6"/></filter>
</defs>
<rect x="${g.outerL - 2}" y="${g.top + 3}" width="${box.w + 4}" height="${box.h - 3}" fill="#000" opacity=".3" filter="url(#sh)"/>
<path d="${hole}" fill="#141517"/>
<g clip-path="url(#bars)">${m.body}</g>
<path d="${outer}" fill="none" stroke="#000" stroke-opacity=".45" stroke-width="1"/>
<path d="M${g.outerL + 1} ${FLOOR} V${g.top + 1} H${g.outerR - 1}" fill="none" stroke="#fff" stroke-opacity=".22" stroke-width="1.2"/>
<path d="M${g.innerL - 1} ${FLOOR} V${LEAF_TOP - 1} H${g.innerR + 1} V${FLOOR}" fill="none" stroke="#000" stroke-opacity=".5" stroke-width="1.5"/>
`);
}

function glassDefs() {
  return (
    '<linearGradient id="gl" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#6f8793"/><stop offset=".55" stop-color="#3a4d57"/><stop offset="1" stop-color="#26343c"/></linearGradient>' +
    '<linearGradient id="gm" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#eef0f2"/><stop offset=".5" stop-color="#9ea4aa"/><stop offset="1" stop-color="#d7dadd"/></linearGradient>' +
    '<linearGradient id="gf" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#e9eef0"/><stop offset="1" stop-color="#bcc7cc"/></linearGradient>'
  );
}

/** Стеклянная вставка в металлической раскладке. frosted — матовое стекло. */
function glassPane(r, { frosted = false, bead = 4 } = {}) {
  const inner = { x: r.x + bead, y: r.y + bead, w: r.w - 2 * bead, h: r.h - 2 * bead };
  const hl = `M${r1(inner.x)} ${r1(inner.y + inner.h * 0.55)} L${r1(inner.x + inner.w * 0.7)} ${r1(inner.y)} L${r1(inner.x + inner.w)} ${r1(inner.y)} L${r1(inner.x)} ${r1(inner.y + inner.h * 0.85)} Z`;
  return (
    rect({ x: r.x - 1, y: r.y - 1, w: r.w + 2, h: r.h + 2 }, 'fill="#000" opacity=".45"') +
    rect(r, 'fill="url(#gm)"') +
    rect(inner, `fill="url(#${frosted ? 'gf' : 'gl'})"`) +
    `<path d="${hl}" fill="#fff" opacity="${frosted ? 0.35 : 0.16}"/>` +
    rect(inner, 'fill="none" stroke="#000" stroke-opacity=".35"')
  );
}

function extrasLayer(size, color, kind) {
  const g = geometry(size);
  const sides = kind === 'sides' || kind === 'both';
  const transom = kind === 'transom' || kind === 'both';
  const leftX = sides ? g.outerL - SIDE_W - FRAME : g.outerL;
  const rightX = sides ? g.outerR + SIDE_W + FRAME : g.outerR;
  const topY = transom ? g.top - TRANSOM_H : g.top;
  const sections = [];
  const panes = [];
  if (sides) {
    sections.push({ x: leftX, y: g.top, w: SIDE_W + FRAME, h: FLOOR - g.top });
    sections.push({ x: g.outerR, y: g.top, w: SIDE_W + FRAME, h: FLOOR - g.top });
    panes.push({ x: leftX + FRAME, y: g.top + FRAME, w: SIDE_W, h: FLOOR - g.top - 2 * FRAME });
    panes.push({ x: g.outerR, y: g.top + FRAME, w: SIDE_W, h: FLOOR - g.top - 2 * FRAME });
  }
  if (transom) {
    sections.push({ x: leftX, y: topY, w: rightX - leftX, h: TRANSOM_H });
    panes.push({ x: leftX + FRAME, y: topY + FRAME, w: rightX - leftX - 2 * FRAME, h: TRANSOM_H - FRAME });
  }
  const box = { x: leftX, y: topY, w: rightX - leftX, h: FLOOR - topY };
  const m = material(`frame-${color}`, box);
  const secPath = sections.map((s) => `M${s.x} ${s.y} h${s.w} v${s.h} h${-s.w} Z`).join(' ');
  let glass = '';
  for (const p of panes) {
    glass += rect(p, 'fill="url(#gl)"');
    glass += `<path d="M${p.x} ${r1(p.y + p.h * 0.5)} L${r1(p.x + p.w)} ${r1(p.y + p.h * 0.3)} L${r1(p.x + p.w)} ${r1(p.y + p.h * 0.36)} L${p.x} ${r1(p.y + p.h * 0.56)} Z" fill="#fff" opacity=".12"/>`;
    glass += rect(p, 'fill="none" stroke="#000" stroke-opacity=".45" stroke-width="1.5"');
  }
  // тонкая раскладка на боковых вставках
  let muntins = '';
  if (sides) {
    for (const p of panes.slice(0, 2)) {
      for (const k of [0.25, 0.5, 0.75]) {
        muntins += rect({ x: p.x, y: p.y + p.h * k - 1.5, w: p.w, h: 3 }, 'fill="#000" opacity=".25"');
      }
    }
  }
  const shadowBox = sections.map((s) => rect({ x: s.x - 2, y: s.y + 3, w: s.w + 4, h: s.h - 3 }, 'fill="#000" opacity=".3"')).join('');
  const floorShadow = sides
    ? `<ellipse cx="${W / 2}" cy="${FLOOR + 8}" rx="${(rightX - leftX) / 2 + 20}" ry="9" fill="#000" opacity=".22" filter="url(#sh)"/>`
    : '';
  return svgDoc(`
<defs>
${m.defs}
${glassDefs()}
<clipPath id="bars"><path d="${secPath}"/></clipPath>
<filter id="sh" x="0" y="0" width="${W}" height="${H}" filterUnits="userSpaceOnUse"><feGaussianBlur stdDeviation="6"/></filter>
</defs>
<g filter="url(#sh)">${shadowBox}</g>
${floorShadow}
<g clip-path="url(#bars)">${m.body}</g>
${glass}
${muntins}
<path d="${secPath}" fill="none" stroke="#000" stroke-opacity=".45"/>
`);
}

function leafShading(r) {
  return (
    rect(r, 'fill="url(#shade)"') +
    rect({ x: r.x + 0.75, y: r.y + 0.75, w: r.w - 1.5, h: r.h - 1.5 }, 'fill="none" stroke="#000" stroke-opacity=".4" stroke-width="1.5"') +
    `<line x1="${r.x + 2}" y1="${r.y + 2}" x2="${r.x + r.w - 2}" y2="${r.y + 2}" stroke="#fff" stroke-opacity=".25"/>`
  );
}

function leafLayer(size, coating) {
  const g = geometry(size);
  const box = bbox(g.leaves);
  const m = material(coating, box, size === 'single' ? 11 : 12);
  const clip = g.leaves.map((r) => rect(r)).join('');
  return svgDoc(`
<defs>
${m.defs}
<clipPath id="leaf">${clip}</clipPath>
<linearGradient id="shade" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#fff" stop-opacity=".08"/><stop offset=".45" stop-color="#fff" stop-opacity="0"/><stop offset="1" stop-color="#000" stop-opacity=".16"/></linearGradient>
</defs>
<g clip-path="url(#leaf)">${m.body}</g>
${g.leaves.map(leafShading).join('\n')}
`);
}

// --- фрезеровка: пути канавок в координатах полотна ---
function millingPaths(kind, r) {
  const d = [];
  const L = r.x;
  const T = r.y;
  const Rr = r.x + r.w;
  const B = r.y + r.h;
  const narrow = r.w < 200;
  switch (kind) {
    case 'lines':
      for (let k = 1; k <= 9; k++) {
        const y = r1(T + (r.h * k) / 10);
        d.push(`M${L + 22} ${y} H${Rr - 22}`);
      }
      break;
    case 'modern': {
      const a = narrow ? 18 : 24;
      const b = narrow ? 34 : 46;
      d.push(`M${L + a} ${T + a} H${Rr - a} V${B - a} H${L + a} Z`);
      d.push(`M${L + b} ${T + b} H${Rr - b} V${B - b} H${L + b} Z`);
      break;
    }
    case 'classic': {
      const side = narrow ? 24 : 34;
      const panels = [
        { t: T + 40, b: T + r.h * 0.44, arch: narrow ? 14 : 26 },
        { t: T + r.h * 0.5, b: B - 44, arch: 0 },
      ];
      for (const p of panels) {
        for (const inset of [0, 12]) {
          const x1 = L + side + inset;
          const x2 = Rr - side - inset;
          const t = r1(p.t + inset);
          const bt = r1(p.b - inset);
          if (p.arch) {
            d.push(`M${x1} ${bt} V${r1(t + p.arch)} Q${r1((x1 + x2) / 2)} ${r1(t - p.arch)} ${x2} ${r1(t + p.arch)} V${bt} Z`);
          } else {
            d.push(`M${x1} ${t} H${x2} V${bt} H${x1} Z`);
          }
        }
      }
      break;
    }
    case 'wave': {
      const n = Math.max(2, Math.floor(r.w / 42));
      for (let i = 1; i <= n; i++) {
        const x0 = L + (r.w * i) / (n + 1);
        const pts = [];
        for (let y = T + 26; y <= B - 26; y += 8) {
          pts.push(`${r1(x0 + Math.sin((y - T) / 38 + i * 0.9) * 9)} ${y}`);
        }
        d.push(`M${pts.join(' L')}`);
      }
      break;
    }
    default:
      throw new Error(`Неизвестная фрезеровка: ${kind}`);
  }
  return d;
}

function grooves(paths) {
  const all = paths.join(' ');
  return (
    `<path d="${all}" fill="none" stroke="#000" stroke-opacity=".45" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round"/>` +
    `<path d="${all}" transform="translate(1.1 1.3)" fill="none" stroke="#fff" stroke-opacity=".24" stroke-width="1.1" stroke-linejoin="round" stroke-linecap="round"/>`
  );
}

function millingLayer(size, kind) {
  const g = geometry(size);
  const paths = g.leaves.flatMap((r) => millingPaths(kind, r));
  return svgDoc(grooves(paths));
}

function glassRects(kind, leaf) {
  switch (kind) {
    case 'vertical':
      return [{ r: { x: leaf.x + 44, y: leaf.y + 64, w: 34, h: 470 } }];
    case 'squares':
      return [0, 1, 2, 3, 4].map((i) => ({ r: { x: leaf.x + 45, y: leaf.y + 80 + i * 64, w: 32, h: 32 } }));
    case 'window':
      return [{ r: { x: leaf.x + 40, y: leaf.y + 44, w: leaf.w - 80, h: 100 }, frosted: true }];
    default:
      throw new Error(`Неизвестное стекло: ${kind}`);
  }
}

function glassLayer(size, kind) {
  const g = geometry(size);
  const panes = glassRects(kind, g.main)
    .map((p) => glassPane(p.r, { frosted: p.frosted }))
    .join('\n');
  return svgDoc(`<defs>${glassDefs()}</defs>\n${panes}`);
}

const FINISH = {
  chrome: ['#fdfdfd', '#b5bbc1', '#eceef0', '#868d94'],
  black: ['#44464a', '#18191b', '#303235', '#0e0f10'],
  gold: ['#fff0ad', '#cf9f3a', '#f5d97f', '#9f7228'],
};

function finishDefs(finish) {
  const [a, b, c, d] = FINISH[finish];
  return (
    `<linearGradient id="hf" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${a}"/><stop offset=".45" stop-color="${b}"/><stop offset=".7" stop-color="${c}"/><stop offset="1" stop-color="${d}"/></linearGradient>` +
    `<linearGradient id="hv" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="${a}"/><stop offset=".45" stop-color="${b}"/><stop offset=".7" stop-color="${c}"/><stop offset="1" stop-color="${d}"/></linearGradient>` +
    '<filter id="hs" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="2"/></filter>'
  );
}

function cylinder(cx, cy) {
  return (
    `<rect x="${cx - 7 + 2}" y="${cy - 19 + 3}" width="14" height="38" rx="7" fill="#000" opacity=".3" filter="url(#hs)"/>` +
    `<rect x="${cx - 7}" y="${cy - 19}" width="14" height="38" rx="7" fill="url(#hv)" stroke="#000" stroke-opacity=".35" stroke-width=".8"/>` +
    `<circle cx="${cx}" cy="${cy - 3}" r="4" fill="#1a1b1d"/>` +
    `<rect x="${cx - 1.3}" y="${cy - 3}" width="2.6" height="11" rx="1.2" fill="#1a1b1d"/>`
  );
}

function leverHandle(hx, hy) {
  const shapes = (dx, dy, fillR, fillL) =>
    `<rect x="${hx - 8 + dx}" y="${hy - 26 + dy}" width="16" height="52" rx="8" fill="${fillR}"/>` +
    `<circle cx="${hx + dx}" cy="${hy + dy}" r="8.5" fill="${fillR}"/>` +
    `<rect x="${hx - 76 + dx}" y="${hy - 6 + dy}" width="80" height="12" rx="6" fill="${fillL}"/>`;
  return (
    `<g filter="url(#hs)" opacity=".35">${shapes(2, 5, '#000', '#000')}</g>` +
    shapes(0, 0, 'url(#hv)', 'url(#hf)') +
    `<rect x="${hx - 8}" y="${hy - 26}" width="16" height="52" rx="8" fill="none" stroke="#000" stroke-opacity=".3" stroke-width=".8"/>` +
    `<rect x="${hx - 76}" y="${hy - 6}" width="80" height="12" rx="6" fill="none" stroke="#000" stroke-opacity=".3" stroke-width=".8"/>`
  );
}

function barHandle(hx, hy) {
  const x = hx + 2;
  const top = hy - 150;
  const bottom = hy + 90;
  return (
    `<g filter="url(#hs)" opacity=".4"><rect x="${x - 6 + 3}" y="${top + 6}" width="12" height="${bottom - top}" rx="6" fill="#000"/></g>` +
    `<rect x="${x - 4}" y="${top + 14}" width="8" height="10" fill="#111"/>` +
    `<rect x="${x - 4}" y="${bottom - 24}" width="8" height="10" fill="#111"/>` +
    `<rect x="${x - 6}" y="${top}" width="12" height="${bottom - top}" rx="6" fill="url(#hv)" stroke="#000" stroke-opacity=".4" stroke-width=".8"/>`
  );
}

function handleLayer(size, kind) {
  const g = geometry(size);
  const hx = g.handleX;
  const hy = HANDLE_Y;
  let body;
  let finish;
  if (kind === 'bar') {
    finish = 'black';
    body = barHandle(hx, hy) + cylinder(hx - 28, hy + 40) + cylinder(hx - 28, hy - 190);
  } else {
    finish = kind;
    body = leverHandle(hx, hy) + cylinder(hx, hy + 68) + cylinder(hx, hy - 190);
  }
  return svgDoc(`<defs>${finishDefs(finish)}</defs>\n${body}`);
}

function peepholeLayer(size, kind) {
  const g = geometry(size);
  const cx = g.main.x + g.main.w / 2;
  const cy = PEEPHOLE_Y;
  let body;
  if (kind === 'standard') {
    body =
      `<circle cx="${cx + 1}" cy="${cy + 2}" r="8" fill="#000" opacity=".3" filter="url(#hs)"/>` +
      `<circle cx="${cx}" cy="${cy}" r="7.5" fill="url(#hv)" stroke="#000" stroke-opacity=".35" stroke-width=".8"/>` +
      `<circle cx="${cx}" cy="${cy}" r="3.6" fill="#1c2a33"/>` +
      `<circle cx="${cx - 1.2}" cy="${cy - 1.2}" r="1.1" fill="#fff" opacity=".7"/>`;
  } else {
    body =
      `<rect x="${cx - 12 + 2}" y="${cy - 18 + 3}" width="24" height="36" rx="5" fill="#000" opacity=".35" filter="url(#hs)"/>` +
      `<rect x="${cx - 12}" y="${cy - 18}" width="24" height="36" rx="5" fill="#1b1c1f" stroke="#55585d" stroke-width="1"/>` +
      `<circle cx="${cx}" cy="${cy - 5}" r="6.5" fill="#0b0c0d" stroke="#3e4146" stroke-width="1.5"/>` +
      `<circle cx="${cx}" cy="${cy - 5}" r="3" fill="#20394a"/>` +
      `<circle cx="${cx - 1}" cy="${cy - 6}" r="1" fill="#fff" opacity=".7"/>` +
      `<circle cx="${cx}" cy="${cy + 10}" r="2" fill="#3fbf6a"/>`;
  }
  return svgDoc(`<defs>${finishDefs('chrome')}</defs>\n${body}`);
}

// ---------- миниатюры 60 × 90 ----------
const TW = 60;
const TH = 90;

function thumbDoc(body, viewBox) {
  return svgDoc(body, { width: TW, height: TH, viewBox: viewBox || `0 0 ${TW} ${TH}` });
}

function materialThumb(kind) {
  const m = material(kind, { x: 0, y: 0, w: TW, h: TH }, 21);
  const frameShade = kind.startsWith('frame-')
    ? '<rect x="0" y="0" width="60" height="90" fill="none" stroke="#fff" stroke-opacity=".25" stroke-width="2"/>'
    : '';
  return thumbDoc(`<defs>${m.defs}</defs>${m.body}${frameShade}`);
}

/** Миниатюра — фрагмент настоящего слоя поверх нейтрального полотна (одностворчатая дверь). */
function cropThumb(layerSvg, crop) {
  const g = geometry('single');
  const inner = layerSvg.replace(/^<svg[^>]*>\n?/, '').replace(/<\/svg>\n?$/, '');
  const leaf = rect(g.main, 'fill="#cfccc6"');
  const vb = `${crop.x} ${crop.y} ${crop.w} ${crop.h}`;
  return svgDoc(`<rect x="${crop.x}" y="${crop.y}" width="${crop.w}" height="${crop.h}" fill="#e7e5e1"/>${leaf}${inner}`, {
    width: TW,
    height: TH,
    viewBox: vb,
  });
}

const LEAF_CROP = (() => {
  const g = geometry('single');
  const h = g.main.h + 40;
  const w = (h * TW) / TH;
  return { x: r1(g.main.x + g.main.w / 2 - w / 2), y: g.main.y - 20, w: r1(w), h };
})();

function iconDoor({ size = 'single', transom = false, sides = false }) {
  const stroke = '#3b3e43';
  const doorW = size === 'single' ? 22 : 32;
  const sideW = sides ? 7 : 0;
  const total = doorW + 2 * sideW;
  const x0 = (TW - total) / 2;
  const top = transom ? 22 : 30;
  let s = '';
  if (transom) s += `<rect x="${x0}" y="${top - 12}" width="${total}" height="12" fill="#b9ccd6" stroke="${stroke}" stroke-width="1.5"/>`;
  if (sides) {
    s += `<rect x="${x0}" y="${top}" width="${sideW}" height="${78 - top}" fill="#b9ccd6" stroke="${stroke}" stroke-width="1.5"/>`;
    s += `<rect x="${x0 + sideW + doorW}" y="${top}" width="${sideW}" height="${78 - top}" fill="#b9ccd6" stroke="${stroke}" stroke-width="1.5"/>`;
  }
  const dx = x0 + sideW;
  s += `<rect x="${dx}" y="${top}" width="${doorW}" height="${78 - top}" fill="#8b8f95" stroke="${stroke}" stroke-width="1.5"/>`;
  if (size !== 'single') s += `<line x1="${dx + 22}" y1="${top}" x2="${dx + 22}" y2="78" stroke="${stroke}" stroke-width="1.5"/>`;
  s += `<rect x="${dx + 16}" y="${top + (78 - top) * 0.52}" width="4" height="2" fill="#f2f2f2"/>`;
  s += `<line x1="8" y1="78.75" x2="52" y2="78.75" stroke="#9a948b" stroke-width="1.5"/>`;
  return thumbDoc(s);
}

// ---------- сборка ----------
const SIZES = ['single', 'oneandhalf'];
const COATINGS = ['graphite', 'copper', 'wenge', 'oak', 'concrete', 'white'];
const MILLINGS = ['lines', 'modern', 'classic', 'wave'];
const GLASSES = ['vertical', 'squares', 'window'];
const HANDLES = ['chrome', 'black', 'gold', 'bar'];
const PEEPHOLES = ['standard', 'video'];
const FRAMES = ['black', 'graphite', 'bronze'];
const EXTRAS = ['transom', 'sides', 'both'];

function main() {
  for (const dir of ['layers', 'thumbs']) {
    fs.rmSync(path.join(ROOT, dir), { recursive: true, force: true });
  }
  let count = 0;
  const out = (rel, svg) => {
    write(rel, svg);
    count++;
  };

  out('layers/scene.svg', sceneLayer());
  for (const size of SIZES) {
    out(`layers/size/${size}.svg`, sizeLayer(size));
    for (const c of COATINGS) out(`layers/coating/${size}/${c}.svg`, leafLayer(size, c));
    for (const m of MILLINGS) out(`layers/milling/${size}/${m}.svg`, millingLayer(size, m));
    for (const gl of GLASSES) out(`layers/glass/${size}/${gl}.svg`, glassLayer(size, gl));
    for (const h of HANDLES) out(`layers/handle/${size}/${h}.svg`, handleLayer(size, h));
    for (const p of PEEPHOLES) out(`layers/peephole/${size}/${p}.svg`, peepholeLayer(size, p));
    for (const f of FRAMES) {
      out(`layers/frame/${size}/${f}.svg`, frameLayer(size, f));
      for (const e of EXTRAS) out(`layers/extras/${size}/${f}/${e}.svg`, extrasLayer(size, f, e));
    }
  }

  // миниатюры
  out('thumbs/size/single.svg', iconDoor({ size: 'single' }));
  out('thumbs/size/oneandhalf.svg', iconDoor({ size: 'oneandhalf' }));
  out('thumbs/extras/none.svg', iconDoor({}));
  out('thumbs/extras/transom.svg', iconDoor({ transom: true }));
  out('thumbs/extras/sides.svg', iconDoor({ sides: true }));
  out('thumbs/extras/both.svg', iconDoor({ transom: true, sides: true }));
  for (const c of COATINGS) out(`thumbs/coating/${c}.svg`, materialThumb(c));
  for (const f of FRAMES) out(`thumbs/frame/${f}.svg`, materialThumb(`frame-${f}`));
  out('thumbs/milling/smooth.svg', cropThumb(svgDoc(''), LEAF_CROP));
  for (const m of MILLINGS) out(`thumbs/milling/${m}.svg`, cropThumb(millingLayer('single', m), LEAF_CROP));
  out('thumbs/glass/none.svg', cropThumb(svgDoc(''), LEAF_CROP));
  for (const gl of GLASSES) out(`thumbs/glass/${gl}.svg`, cropThumb(glassLayer('single', gl), LEAF_CROP));
  const hx = geometry('single').handleX;
  const handleCrop = { x: hx - 96, y: HANDLE_Y - 150, w: 140, h: 210 };
  for (const h of HANDLES) out(`thumbs/handle/${h}.svg`, cropThumb(handleLayer('single', h), handleCrop));
  const pc = { x: W / 2 - 30, y: PEEPHOLE_Y - 45, w: 60, h: 90 };
  out('thumbs/peephole/none.svg', cropThumb(svgDoc(''), pc));
  for (const p of PEEPHOLES) out(`thumbs/peephole/${p}.svg`, cropThumb(peepholeLayer('single', p), pc));

  console.log(`Готово: ${count} файлов в layers/ и thumbs/`);
}

if (require.main === module) main();

module.exports = { geometry };
