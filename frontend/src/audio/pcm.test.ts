import { describe, expect, it } from "vitest";
import { ByteRing, FRAME_BYTES, FRAME_SAMPLES, framePcm16, outputPcmAtContextRate, pcm16le, resampleLinear } from "./pcm";

describe("live PCM boundary", () => {
  it("resamples one second to exact 16 kHz and emits 20 ms frames", () => {
    const source = new Float32Array(48_000).fill(0.5);
    const samples = resampleLinear(source, 48_000, 16_000);
    expect(samples).toHaveLength(16_000);
    const bytes = pcm16le(samples);
    const frames = framePcm16(bytes);
    expect(frames).toHaveLength(50);
    expect(frames.every((frame) => frame.byteLength === FRAME_BYTES)).toBe(true);
    expect(FRAME_SAMPLES).toBe(320);
  });

  it("bounds audio queues and clears playback synchronously", () => {
    const ring = new ByteRing(640);
    ring.push(new Uint8Array(400));
    ring.push(new Uint8Array(400));
    expect(ring.byteLength).toBeLessThanOrEqual(640);
    ring.clear();
    expect(ring.byteLength).toBe(0);
  });

  it("converts returned 24 kHz PCM to the browser AudioContext rate", () => {
    const providerPcm = pcm16le(new Float32Array(240).fill(0.25));
    const browserSamples = outputPcmAtContextRate(providerPcm, 48_000);
    expect(browserSamples).toHaveLength(480);
    expect(browserSamples[100]).toBeCloseTo(0.25, 3);
  });
});
