import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { profilePanel } from "../../redibis/webapp/static/steward_review.mjs";

const GENERIC = "No profile stats for this column yet";

describe("steward profilePanel", () => {
  it("not_run renders the scan-type explanation, not the generic hint", () => {
    const html = profilePanel({ profile: {
      stats: { logical_type: "string" }, profile_status: "not_run",
      profile_run_id: "run-42", profile_engines: [],
    } });
    assert.match(html, /Profiling was not part of the scan/);
    assert.match(html, /run run-42 · no engines recorded/);
    assert.ok(!html.includes(GENERIC));
  });

  it("no_run and write_failed render their own messages", () => {
    assert.match(profilePanel({ profile: { profile_status: "no_run" } }),
      /No profile has been captured for this table yet/);
    assert.match(profilePanel({ profile: { profile_status: "write_failed", profile_run_id: "r" } }),
      /could not be stored/);
  });

  it("null_rate of 0 with ndv present does not render the empty hint", () => {
    const html = profilePanel({ profile: {
      stats: { null_rate: 0, ndv: 12, logical_type: "string" },
      profile_status: "ok", profile_run_id: "r1", profile_engines: ["profile"],
    } });
    assert.ok(!html.includes('data-profile-status'));
    assert.ok(!html.includes(GENERIC));
    assert.match(html, /0\.0%/);
    assert.match(html, /engines: profile/);
  });

  it("ok status renders no hint", () => {
    const html = profilePanel({ profile: { stats: {}, profile_status: "ok" } });
    assert.ok(!html.includes("data-profile-status"));
  });

  it("escapes server-provided run ids and engines", () => {
    const html = profilePanel({ profile: {
      profile_status: "ok", profile_run_id: "<x>", profile_engines: ["<e>"],
    } });
    assert.ok(!html.includes("<x>"));
    assert.ok(!html.includes("<e>"));
  });
});
