// Shared helpers of the node:test unit suites (test/*.test.mjs).
import { after } from "node:test";
import assert from "node:assert/strict";

// Fails the calling suite at its end if any promise rejection went unhandled during it.
export function failOnUnhandledRejections() {
  let unhandled = 0;
  process.on("unhandledRejection", () => { unhandled++; });
  after(() => assert.equal(unhandled, 0, "unhandled rejections"));
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
