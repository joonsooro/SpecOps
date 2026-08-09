export const INPUT_RATE = 16_000;
export const OUTPUT_RATE = 24_000;
export const FRAME_SAMPLES = 320;
export const FRAME_BYTES = 640;

export function resampleLinear(input: Float32Array, sourceRate: number, targetRate: number): Float32Array {
  if (sourceRate <= 0 || targetRate <= 0) throw new Error("sample rates must be positive");
  if (input.length === 0) return new Float32Array();
  if (sourceRate === targetRate) return input.slice();
  const length = Math.max(1, Math.floor((input.length * targetRate) / sourceRate));
  const output = new Float32Array(length);
  const ratio = sourceRate / targetRate;
  for (let index = 0; index < length; index += 1) {
    const position = index * ratio;
    const left = Math.min(input.length - 1, Math.floor(position));
    const right = Math.min(input.length - 1, left + 1);
    const fraction = position - left;
    output[index] = input[left] * (1 - fraction) + input[right] * fraction;
  }
  return output;
}

export function pcm16le(samples: Float32Array): Uint8Array {
  const output = new Uint8Array(samples.length * 2);
  const view = new DataView(output.buffer);
  samples.forEach((sample, index) => {
    const bounded = Math.max(-1, Math.min(1, sample));
    view.setInt16(index * 2, bounded < 0 ? bounded * 0x8000 : bounded * 0x7fff, true);
  });
  return output;
}

export function outputPcmAtContextRate(input: Uint8Array, contextRate: number): Float32Array {
  if (input.byteLength % 2 !== 0) throw new Error("PCM16 input must contain whole samples");
  const view = new DataView(input.buffer, input.byteOffset, input.byteLength);
  const source = new Float32Array(input.byteLength / 2);
  for (let index = 0; index < source.length; index += 1) source[index] = view.getInt16(index * 2, true) / 0x8000;
  return resampleLinear(source, OUTPUT_RATE, contextRate);
}

export function framePcm16(input: Uint8Array): Uint8Array[] {
  if (input.byteLength % 2 !== 0) throw new Error("PCM16 input must contain whole samples");
  const frames: Uint8Array[] = [];
  for (let offset = 0; offset + FRAME_BYTES <= input.byteLength; offset += FRAME_BYTES) {
    frames.push(input.slice(offset, offset + FRAME_BYTES));
  }
  return frames;
}

export class ByteRing {
  readonly capacity: number;
  private chunks: Uint8Array[] = [];
  byteLength = 0;

  constructor(capacity: number) {
    if (capacity <= 0) throw new Error("capacity must be positive");
    this.capacity = capacity;
  }

  push(chunk: Uint8Array): void {
    const retained = chunk.byteLength > this.capacity ? chunk.slice(chunk.byteLength - this.capacity) : chunk.slice();
    this.chunks.push(retained);
    this.byteLength += retained.byteLength;
    while (this.byteLength > this.capacity && this.chunks.length) {
      this.byteLength -= this.chunks.shift()!.byteLength;
    }
  }

  shift(): Uint8Array | undefined {
    const value = this.chunks.shift();
    if (value) this.byteLength -= value.byteLength;
    return value;
  }

  clear(): void {
    this.chunks = [];
    this.byteLength = 0;
  }
}
