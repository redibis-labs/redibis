import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  buildCategoryPatch,
  normalizeEntityType,
  splitTriggers,
  triggerPattern,
} from "../../redibis/webapp/static/gateway_rules.mjs";

describe("custom PII category patch", () => {
  it("folds a social URL name and trigger wording into a free-text pattern", () => {
    const patch = buildCategoryPatch("social URL", "instagram, linkedin.com", "");
    assert.equal(patch.entity_type, "SOCIAL_URL");
    assert.deepEqual(patch.triggers, ["instagram", "linkedin.com"]);
    const ig = patch.patternsAdd.social_url_instagram;
    assert.ok(ig);
    assert.equal(ig.entity_type, "SOCIAL_URL");
    assert.equal(ig.recognizer_group, "free_text");
    assert.equal(ig.pattern, triggerPattern("instagram"));
    assert.match(patch.patternsAdd.social_url_linkedin_com.pattern, /linkedin\\.com/);
  });

  it("requires a category name", () => {
    const patch = buildCategoryPatch("  ", "instagram", "");
    assert.equal(patch.entity_type, "");
    assert.deepEqual(patch.patternsAdd, {});
  });

  it("splits comma and newline trigger lists", () => {
    assert.deepEqual(splitTriggers("instagram, twitter\nlinkedin.com"), [
      "instagram", "twitter", "linkedin.com",
    ]);
    assert.equal(normalizeEntityType("social url"), "SOCIAL_URL");
  });
});
