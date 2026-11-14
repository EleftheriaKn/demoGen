// Records a rough ~3 minute "screen recording" of TodoMVC (React) in Chrome.
// Usage: NODE_PATH=<recdeps>/node_modules node record.js <out_dir>
const fs = require("fs");
const path = require("path");
const puppeteer = require("puppeteer-core");

const OUT = process.argv[2];
const FRAMES = path.join(OUT, "frames");
fs.mkdirSync(FRAMES, { recursive: true });
const URL = process.env.TODO_URL || "https://todomvc.com/examples/javascript-es6/dist/";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = [];
let t0;
const mark = (what) => { const t = (Date.now() - t0) / 1000; log.push({ t: +t.toFixed(1), what }); console.log(t.toFixed(1), what); };

// A visible cursor (headless Chrome draws none) and a click ripple.
const CURSOR = `
  window.addEventListener('DOMContentLoaded', () => {
    const c = document.createElement('div');
    c.style.cssText = 'position:fixed;left:0;top:0;width:22px;height:22px;z-index:2147483647;pointer-events:none;' +
      'background:url("data:image/svg+xml;utf8,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 22 22%22><path d=%22M2 2 L2 18 L7 13 L10 20 L13 19 L10 12 L17 12 Z%22 fill=%22black%22 stroke=%22white%22 stroke-width=%221.5%22/></svg>") no-repeat;';
    document.body.appendChild(c);
    const pos = JSON.parse(sessionStorage.getItem('__cur') || '[960,540]');
    c.style.transform = 'translate(' + pos[0] + 'px,' + pos[1] + 'px)';
    document.addEventListener('mousemove', e => {
      c.style.transform = 'translate(' + e.clientX + 'px,' + e.clientY + 'px)';
      sessionStorage.setItem('__cur', JSON.stringify([e.clientX, e.clientY]));
    }, true);
    document.addEventListener('mousedown', e => {
      const r = document.createElement('div');
      r.style.cssText = 'position:fixed;z-index:2147483646;pointer-events:none;border-radius:50%;border:3px solid rgba(255,140,0,.8);' +
        'width:10px;height:10px;left:' + (e.clientX - 5) + 'px;top:' + (e.clientY - 5) + 'px;transition:all .4s ease-out;';
      document.body.appendChild(r);
      requestAnimationFrame(() => { r.style.width = r.style.height = '40px'; r.style.left = (e.clientX - 20) + 'px'; r.style.top = (e.clientY - 20) + 'px'; r.style.opacity = 0; });
      setTimeout(() => r.remove(), 500);
    }, true);
  });`;

(async () => {
  const browser = await puppeteer.launch({
    executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    headless: true,
    args: ["--window-size=1920,1080", "--hide-scrollbars", "--force-device-scale-factor=1"],
    defaultViewport: { width: 1920, height: 1080, deviceScaleFactor: 1 },
  });
  const page = await browser.newPage();
  await page.evaluateOnNewDocument(CURSOR);
  let mx = 960, my = 540;
  const move = async (x, y, ms = 600) => { const steps = Math.max(8, Math.round(ms / 16)); await page.mouse.move(x, y, { steps }); mx = x; my = y; };
  const moveTo = async (sel, ms) => { const b = await (await page.$(sel)).boundingBox(); await move(b.x + b.width / 2, b.y + b.height / 2, ms); return b; };
  const click = async (sel, ms = 700) => { await moveTo(sel, ms); await sleep(150); await page.mouse.down(); await sleep(80); await page.mouse.up(); };
  const type = async (text, base = 110) => { for (const ch of text) { await page.keyboard.type(ch); await sleep(base + Math.random() * base); } };
  const backspace = async (n) => { for (let i = 0; i < n; i++) { await page.keyboard.press("Backspace"); await sleep(140); } };
  const wander = async (ms) => { const end = Date.now() + ms; while (Date.now() < end) { await move(300 + Math.random() * 1300, 200 + Math.random() * 700, 900); await sleep(300 + Math.random() * 900); } };
  const nthToggle = (n) => `ul.todo-list li:nth-child(${n}) input.toggle`;
  const nthLabel = (n) => `ul.todo-list li:nth-child(${n}) label`;

  // Start recording on a blank tab, like a real take that starts before the app is open.
  await page.goto("about:blank");
  const cdp = await page.createCDPSession();
  const frames = [];
  cdp.on("Page.screencastFrame", async ({ data, metadata, sessionId }) => {
    const file = path.join(FRAMES, `f${String(frames.length).padStart(6, "0")}.jpg`);
    fs.writeFileSync(file, Buffer.from(data, "base64"));
    frames.push({ file, ts: metadata.timestamp });
    try { await cdp.send("Page.screencastFrameAck", { sessionId }); } catch (e) {}
  });
  await cdp.send("Page.startScreencast", { format: "jpeg", quality: 88, maxWidth: 1920, maxHeight: 1080, everyNthFrame: 1 });
  t0 = Date.now();
  const startTs = Date.now() / 1000;

  mark("dead: blank tab, recording started");
  await sleep(4000);
  mark("setup: open TodoMVC");
  await page.goto(URL, { waitUntil: "networkidle2" });
  await page.evaluate(() => localStorage.clear());
  await page.reload({ waitUntil: "networkidle2" });
  await wander(5000);                                   // dead: looking around

  mark("action: add first todos");
  await click("input.new-todo");
  await type("Buy milk"); await page.keyboard.press("Enter"); await sleep(900);
  await type("Finish the hackathon demo vidoe"); await sleep(600); await backspace(3); await type("deo"); await page.keyboard.press("Enter"); await sleep(900);
  await type("Call mom"); await page.keyboard.press("Enter"); await sleep(800);
  await type("Fix lgoin bug"); await sleep(700); await backspace(9); await type("login bug"); await page.keyboard.press("Enter");
  mark("result: four todos, counter says 4 items left");
  await sleep(2500);

  mark("dead: idle, mouse wandering");
  await wander(8000);

  mark("action: complete two todos");
  await click(nthToggle(1)); await sleep(1200);
  await click(nthToggle(3)); await sleep(1500);
  mark("result: two crossed out, 2 items left");
  await sleep(2500);

  mark("action: filters Active / Completed / All");
  await click('a[href="#/active"]'); await sleep(2500);
  await click('a[href="#/completed"]'); await sleep(2500);
  await click('a[href="#/"]'); await sleep(2000);
  mark("result: filters show the right lists");

  mark("dead: idle");
  await wander(10000);

  mark("action: edit a todo by double-click");
  const lb = await moveTo(nthLabel(2), 700);
  await sleep(300);
  await page.mouse.click(lb.x + lb.width / 2, lb.y + lb.height / 2, { count: 2 });
  await sleep(800);
  await page.keyboard.down("Meta"); await page.keyboard.press("a"); await page.keyboard.up("Meta");
  await page.keyboard.press("End");
  await type(" (2 min max)"); await sleep(500); await page.keyboard.press("Enter");
  mark("result: todo renamed");
  await sleep(2500);

  mark("action: delete a todo with the x button");
  const row = await moveTo("ul.todo-list li:nth-child(4) label", 700);
  await sleep(900);
  await click("ul.todo-list li:nth-child(4) button.destroy", 500);
  mark("result: todo deleted");
  await sleep(2500);

  mark("dead: idle, scrolling around");
  await wander(9000);

  mark("action: add more todos");
  await click("input.new-todo");
  for (const t of ["Book flights", "Water the plants", "Reply to Nikos", "Pay electricity bill"]) {
    await type(t, 90); await page.keyboard.press("Enter"); await sleep(700);
  }
  mark("result: six todos");
  await sleep(2500);

  mark("action: mark all as complete");
  await click("input.toggle-all + label, label[for='toggle-all']"); await sleep(2500);
  mark("result: everything crossed out, 0 items left, Clear completed appears");
  await sleep(2000);
  await click("input.toggle-all + label, label[for='toggle-all']"); await sleep(2000);
  mark("action: undo, then complete three");
  await click(nthToggle(1)); await sleep(700);
  await click(nthToggle(4)); await sleep(700);
  await click(nthToggle(5)); await sleep(1500);

  mark("action: clear completed");
  await click("button.clear-completed"); await sleep(2500);
  mark("result: completed todos removed");

  mark("dead: idle");
  await wander(10000);

  mark("action: add one last todo");
  await click("input.new-todo");
  await type("Submit before 18:00"); await page.keyboard.press("Enter"); await sleep(1500);
  mark("result: new todo added at the top, counter updated");
  await sleep(2000);

  mark("dead: end of take");
  const target = 180;                                  // pad to ~3 min of mostly idle
  while ((Date.now() - t0) / 1000 < target) await wander(3000);

  const endTs = Date.now() / 1000;
  await cdp.send("Page.stopScreencast");
  await browser.close();

  // ffconcat list: each frame lasts until the next one; the last until the end.
  const lines = ["ffconcat version 1.0"];
  frames.forEach((f, i) => {
    const next = i + 1 < frames.length ? frames[i + 1].ts : endTs;
    lines.push(`file '${f.file}'`, `duration ${Math.max(0.001, next - f.ts).toFixed(3)}`);
  });
  lines.push(`file '${frames[frames.length - 1].file}'`);
  fs.writeFileSync(path.join(OUT, "frames.ffconcat"), lines.join("\n") + "\n");
  fs.writeFileSync(path.join(OUT, "log.json"), JSON.stringify({ first_frame_offset_s: frames[0].ts - startTs, log }, null, 1));
  console.log("frames", frames.length, "length", (endTs - frames[0].ts).toFixed(1), "s");
})().catch((e) => { console.error(e); process.exit(1); });
