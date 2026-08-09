class SpecOpsPlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.port.onmessage = (message) => {
      if (message.data.type === "clear") this.queue = [];
      if (message.data.type === "audio") {
        const view = new DataView(message.data.data);
        const source = new Float32Array(view.byteLength / 2);
        for (let index = 0; index < source.length; index += 1) source[index] = view.getInt16(index * 2, true) / 0x8000;
        const length = Math.floor((source.length * sampleRate) / 24000);
        for (let index = 0; index < length; index += 1) {
          const position = index * 24000 / sampleRate;
          const left = Math.floor(position);
          const right = Math.min(source.length - 1, left + 1);
          const fraction = position - left;
          this.queue.push(source[left] * (1 - fraction) + source[right] * fraction);
        }
        if (this.queue.length > sampleRate * 10) this.queue.splice(0, this.queue.length - sampleRate * 10);
      }
    };
  }

  process(_inputs, outputs) {
    const output = outputs[0]?.[0];
    if (!output) return true;
    for (let index = 0; index < output.length; index += 1) output[index] = this.queue.shift() ?? 0;
    return true;
  }
}

registerProcessor("specops-playback", SpecOpsPlaybackProcessor);
