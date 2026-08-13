export class WorkshopPresenceClient {
  private socket: WebSocket | null = null;
  private reconnectTimer: number | null = null;
  private stopped = true;

  start(): void {
    this.stopped = false;
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.reconnectTimer !== null) window.clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.socket?.close(1000, "workshop page closed");
    this.socket = null;
  }

  private connect(): void {
    if (this.stopped || this.socket !== null) return;
    this.socket = new WebSocket(
      `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/presence`,
    );
    this.socket.onclose = () => {
      this.socket = null;
      if (!this.stopped) {
        this.reconnectTimer = window.setTimeout(() => {
          this.reconnectTimer = null;
          this.connect();
        }, 1000);
      }
    };
  }
}
