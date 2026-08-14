import { FRAME_BYTES } from "./pcm";

export function pcmFrameHasSpeech(frame: ArrayBuffer, threshold = 900): boolean {
  const samples = new Int16Array(frame);
  if (samples.length === 0) return false;
  let energy = 0;
  for (const sample of samples) energy += sample * sample;
  return Math.sqrt(energy / samples.length) >= threshold;
}

export class LiveAudioClient {
  private socket: WebSocket | null = null;
  private context: AudioContext | null = null;
  private capture: AudioWorkletNode | null = null;
  private playback: AudioWorkletNode | null = null;
  private stream: MediaStream | null = null;
  private playbackActive = false;

  private clearPlaybackForSpeech(): void {
    if (!this.playbackActive) return;
    this.playback?.port.postMessage({ type: "clear" });
    this.playbackActive = false;
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify({ type: "INTERRUPT" }));
    }
  }

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
      if (message.data.byteLength !== FRAME_BYTES || this.socket?.readyState !== WebSocket.OPEN) return;
      if (pcmFrameHasSpeech(message.data)) this.clearPlaybackForSpeech();
      this.socket.send(message.data);
    };
    this.socket.onmessage = (message) => {
      if (message.data instanceof ArrayBuffer) {
        this.playbackActive = true;
        this.playback?.port.postMessage({ type: "audio", data: message.data }, [message.data]);
      } else {
        const value = JSON.parse(message.data) as Record<string, unknown>;
        if (value.type === "INTERRUPTED" || (value.type === "CALL_STATE" && value.state === "LISTENING")) {
          this.playbackActive = false;
        }
        onEvent(value);
        if (value.type === "CALL_STATE" && value.state === "ENDED") {
          const endedSocket = this.socket;
          this.socket = null;
          endedSocket?.close(1000, "call ended");
        }
      }
    };
    this.stream.getAudioTracks().forEach((track) => {
      track.onended = () => {
        if (this.socket?.readyState === WebSocket.OPEN) {
          this.socket.send(JSON.stringify({ type: "DEVICE_FAILURE" }));
        }
      };
    });
  }

  interrupt(): void {
    // Client-first barge-in: clear audible playback before the server/provider signal.
    this.playback?.port.postMessage({ type: "clear" });
    this.playbackActive = false;
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify({ type: "INTERRUPT" }));
  }

  sendText(text: string, turnSequence: number, providerRequestId: string): void {
    this.socket?.send(JSON.stringify({ type: "TEXT", text, turn_sequence: turnSequence, provider_request_id: providerRequestId }));
  }

  setMuted(muted: boolean): void {
    this.stream?.getAudioTracks().forEach((track) => { track.enabled = !muted; });
  }

  sendControl(intent: "CONFIRM" | "EDIT" | "REJECT", proposalRef: string, editInstruction: string | null = null): void {
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify({
        type: "CONTROL",
        intent,
        proposal_ref: proposalRef,
        edit_instruction: editInstruction,
        acknowledgement: intent === "CONFIRM" ? "Confirmed" : null,
      }));
    }
  }

  end(): void {
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify({ type: "END" }));
    this.stream?.getTracks().forEach((track) => track.stop());
    void this.context?.close();
  }
}
