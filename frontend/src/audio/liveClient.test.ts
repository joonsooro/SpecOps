import { describe, expect, it } from "vitest";
import { pcmFrameHasSpeech } from "./liveClient";

describe("client-first voice activity barge-in", () => {
  it("distinguishes speech energy from silence within one 20 ms frame", () => {
    const silence = new Int16Array(320);
    const speech = new Int16Array(320).fill(2400);
    expect(pcmFrameHasSpeech(silence.buffer)).toBe(false);
    expect(pcmFrameHasSpeech(speech.buffer)).toBe(true);
  });
});
