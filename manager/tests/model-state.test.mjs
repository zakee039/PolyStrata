import assert from "node:assert/strict";
import test from "node:test";
import { modelIsLoaded } from "../src/model-state.ts";

test("manual stop displays Start and triggers load, not unload", () => {
  assert.equal(modelIsLoaded(true, "paused"), false);
});
test("idle-unloaded model can be loaded", () => {
  assert.equal(modelIsLoaded(true, "unloaded"), false);
});
test("loaded model offers unload", () => {
  for (const phase of ["idle","reading","generating"]) {
    assert.equal(modelIsLoaded(true, phase), true, phase);
  }
});
test("external or failed API never reports a loaded managed model", () => {
  for(const phase of ["stopped","starting","stopping","error","external"]) {
    assert.equal(modelIsLoaded(true, phase), false, phase);
  }
  assert.equal(modelIsLoaded(false, "idle"), false);
});
