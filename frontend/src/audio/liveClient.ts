import { FRAME_BYTES } from "./pcm";

export class LiveAudioClient {
  private socket: WebSocket | null = null;
  private context: AudioContext | null = null;
  private capture: AudioWorkletNode | null = null;
  private playback: AudioWorkletNode | null = null;
  private stream: MediaStream | null = null;

  async start(onEvent: (value: unknown) => void): Promise<void> {
    this.context = new AudioContext();
    await Promise.all([
      this.context.audioWorklet.addModule("/audio-capture-worklet.js"),
      this.context.audioWorklet.addModule("/audio-playback-worklet.js"),
    ]);
    this.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 } });
    this.capture = new AudioWorkletNode(this.context, "specops-capture");
    this.playback = new AudioWorkletNode(this.context, "specops-playback");
    this.context.createMediaStreamSource(this.stream).connect(this.capture);
    this.playback.connect(this.context.destination);
    this.socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/live`);
    this.socket.binaryType = "arraybuffer";
    this.capture.port.onmessage = (message: MessageEvent<ArrayBuffer>) => {
      if (message.data.byteLength === FRAME_BYTES && this.socket?.readyState === WebSocket.OPEN) this.socket.send(message.data);
    };
    this.socket.onmessage = (message) => {
      if (message.data instanceof ArrayBuffer) this.playback?.port.postMessage({ type: "audio", data: message.data }, [message.data]);
      else onEvent(JSON.parse(message.data));
    };
  }

  interrupt(): void {
    // Client-first barge-in: clear audible playback before the server/provider signal.
    this.playback?.port.postMessage({ type: "clear" });
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify({ type: "INTERRUPT" }));
  }

  sendText(text: string, turnSequence: number, providerRequestId: string): void {
    this.socket?.send(JSON.stringify({ type: "TEXT", text, turn_sequence: turnSequence, provider_request_id: providerRequestId }));
  }

  end(): void {
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify({ type: "END" }));
    this.stream?.getTracks().forEach((track) => track.stop());
    void this.context?.close();
  }
}
