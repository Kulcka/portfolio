'use strict';

// Проверки файлов проекта: все картинки из config.js на месте, размеры холста совпадают,
// лишних файлов нет, тексты в UTF-8 с переводами строк LF.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const core = require('../configurator.js');
const config = require('../config.js');

const ROOT = path.resolve(__dirname, '..');

function walk(dir) {
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walk(full));
    else out.push(full);
  }
  return out;
}

const rel = (file) => path.relative(ROOT, file).split(path.sep).join('/');

test('каждая картинка из конфига существует на диске', () => {
  const paths = core.listImagePaths(config);
  assert.ok(paths.length > 50, `картинок подозрительно мало: ${paths.length}`);
  const missing = paths.filter((p) => !fs.existsSync(path.join(ROOT, p)));
  assert.deepEqual(missing, [], 'нет файлов');
});

test('у каждой группы есть default, и он среди вариантов', () => {
  for (const g of config.groups) {
    assert.ok(g.default, `у группы ${g.id} нет default`);
    assert.ok(
      g.options.some((o) => o.id === g.default),
      `default группы ${g.id} не найден`
    );
  }
});

/** Размер картинки: SVG — из атрибутов width/height, PNG — из заголовка IHDR. */
function imageSize(file) {
  const buf = fs.readFileSync(file);
  if (file.endsWith('.png')) {
    assert.equal(buf.toString('ascii', 12, 16), 'IHDR', `${file}: не PNG`);
    return { w: buf.readUInt32BE(16), h: buf.readUInt32BE(20) };
  }
  if (file.endsWith('.svg')) {
    const head = buf.toString('utf8', 0, 400);
    const m = head.match(/<svg[^>]*\swidth="(\d+(?:\.\d+)?)"[^>]*\sheight="(\d+(?:\.\d+)?)"/);
    assert.ok(m, `${file}: у SVG нет width/height`);
    return { w: Number(m[1]), h: Number(m[2]) };
  }
  return null; // другие форматы размер не проверяем
}

test('все слои — одного размера с холстом (canvas), миниатюры — пропорции 2 : 3', () => {
  const { width, height } = config.canvas;
  for (const p of core.listImagePaths(config)) {
    const size = imageSize(path.join(ROOT, p));
    if (!size) continue;
    if (p.startsWith('thumbs/') || !isLayer(p)) {
      assert.ok(Math.abs(size.w / size.h - 2 / 3) < 0.01, `${p}: миниатюра ${size.w}×${size.h}, нужно 2:3`);
    } else {
      assert.deepEqual(size, { w: width, h: height }, `${p}: слой не совпадает с холстом ${width}×${height}`);
    }
  }
});

function isLayer(p) {
  const layerPaths = new Set();
  for (const l of config.staticLayers || []) layerPaths.add(l.image);
  for (const g of config.groups) for (const o of g.options) if (o.image) layerPaths.add(o.image);
  return [...layerPaths].some((t) => new RegExp('^' + t.replace(/[.*+?^$()|[\]\\]/g, '\\$&').replace(/\\?\{[^}]+\\?\}/g, '[^/]+') + '$').test(p));
}

test('в layers/ и thumbs/ нет файлов, которые не используются в конфиге', () => {
  const used = new Set(core.listImagePaths(config));
  const onDisk = [...walk(path.join(ROOT, 'layers')), ...walk(path.join(ROOT, 'thumbs'))].map(rel);
  const orphans = onDisk.filter((p) => !used.has(p));
  assert.deepEqual(orphans, []);
});

test('SVG-файлы — корректный XML без очевидных поломок', () => {
  for (const p of core.listImagePaths(config).filter((f) => f.endsWith('.svg'))) {
    const text = fs.readFileSync(path.join(ROOT, p), 'utf8');
    assert.ok(text.trimEnd().endsWith('</svg>'), `${p}: файл обрезан`);
    assert.ok(!/NaN|undefined|Infinity/.test(text), `${p}: в разметке NaN/undefined`);
    const opened = (text.match(/<(?:g|defs|clipPath|filter|linearGradient|radialGradient|svg)[\s>]/g) || []).length;
    const closed = (text.match(/<\/(?:g|defs|clipPath|filter|linearGradient|radialGradient|svg)>/g) || []).length;
    assert.equal(opened, closed, `${p}: не закрыты теги`);
  }
});

test('index.html подключает стили, конфиг и скрипт в правильном порядке', () => {
  const html = fs.readFileSync(path.join(ROOT, 'index.html'), 'utf8');
  assert.ok(html.includes('href="configurator.css"'));
  assert.ok(html.includes('data-door-configurator'));
  const cfgPos = html.indexOf('src="config.js"');
  const jsPos = html.indexOf('src="configurator.js"');
  assert.ok(cfgPos > 0 && jsPos > cfgPos, 'config.js должен идти до configurator.js');
  assert.ok(!/https?:\/\/(?!www\.w3\.org)/.test(html), 'без внешних адресов (CDN)');
});

test('тексты проекта — UTF-8 с LF, без BOM и без заглушек', () => {
  const files = walk(ROOT)
    .map(rel)
    .filter((p) => /\.(js|css|html|md|svg)$/.test(p));
  assert.ok(files.length > 10);
  for (const p of files) {
    const buf = fs.readFileSync(path.join(ROOT, p));
    assert.ok(!(buf[0] === 0xef && buf[1] === 0xbb && buf[2] === 0xbf), `${p}: BOM`);
    const text = buf.toString('utf8');
    assert.ok(!text.includes(String.fromCharCode(0xfffd)), `${p}: не UTF-8`);
    assert.ok(!text.includes('\r'), `${p}: CRLF`);
    if (!p.startsWith('tests/')) assert.ok(!/lorem ipsum|TODO|FIXME/i.test(text), `${p}: заглушка`);
  }
});
