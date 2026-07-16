import fs from "node:fs";
import path from "node:path";

const root = path.resolve(process.cwd(), "..");
const inputs = JSON.parse(process.env.RUNX_INPUTS_JSON || "{}");
const siteDir = String(inputs.site_dir || "public-docs");
const commit = String(inputs.target_commit || "");
const expected = [
  "index.html",
  "limitless-hl-bot/overview.html",
  "limitless-hl-bot/market-discovery.html",
  "limitless-hl-bot/scoring-and-signals.html",
  "limitless-hl-bot/execution-and-orders.html",
  "limitless-hl-bot/risk-and-exits.html",
  "limitless-hl-bot/operations-and-learning.html"
];
const read = (p) => fs.readFileSync(path.join(root, p), "utf8");
const missing = expected.filter((p) => !fs.existsSync(path.join(root, siteDir, p)));
const html = expected.filter((p) => !missing.includes(p)).map((p) => read(path.join(siteDir, p))).join("\n");
const links = (html.match(new RegExp(`github\\.com/samlogic-ship/limitless-hl-bot/blob/${commit}`, "g")) || []).length;
const observations = [];
if (missing.length) observations.push(`missing pages: ${missing.join(", ")}`);
if (!html.includes(commit)) observations.push("pinned commit absent from rendered HTML");
if (links < 20) observations.push(`source link count ${links} is below 20`);
const report = {
  schema: "sourcey.limitless_site_validation.v1",
  target: { repo: "samlogic-ship/limitless-hl-bot", commit, license: "MIT" },
  site_dir: siteDir,
  expected_pages: expected,
  missing_pages: missing,
  source_link_count: links,
  valid: observations.length === 0,
  observations
};
console.log(JSON.stringify({ validation_report: report }, null, 2));
if (!report.valid) process.exit(64);
