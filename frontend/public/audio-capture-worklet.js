class SpecOpsCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.pending = [];
    this.position = 0;
  }

  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input) return true;
    const ratio = sampleRate / 16000;
    while (this.position < input.length) {
      const left = Math.floor(this.position);
      const right = Math.min(input.length - 1, left + 1);
      const fraction = this.position - left;
      this.pending.push(input[left] * (1 - fraction) + input[right] * fraction);
      this.position += ratio;
    }
    this.position -= input.length;
    while (this.pending.length >= 320) {
      const buffer = new ArrayBuffer(640);
      const view = new DataView(buffer);
      for (let index = 0; index < 320; index += 1) {
        const sample = Math.max(-1, Math.min(1, this.pending[index]));
        view.setInt16(index * 2, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
      }
      this.pending.splice(0, 320);
      this.port.postMessage(buffer, [buffer]);
    }
    return true;
  }
}

registerProcessor("specops-capture", SpecOpsCaptureProcessor);
