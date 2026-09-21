/** 三类设置请求各自只接受最新回复，不依赖 DOM 或持久化。 */
export type ProviderChannel = "models-fetch" | "mp-models" | "probe-capabilities";

export class ProviderRequests {
  private sequence = 0;
  private pending = new Map<ProviderChannel, { id: string; identity: string }>();

  begin(channel: ProviderChannel, identity: string): string {
    const id = `${channel}-${++this.sequence}`;
    this.pending.set(channel, { id, identity });
    return id;
  }

  owns(channel: ProviderChannel, id: string): boolean {
    return id.startsWith(`${channel}-`);
  }

  take(channel: ProviderChannel, id: string, identity: () => string): boolean {
    const pending = this.pending.get(channel);
    if (!pending || pending.id !== id) return false;
    this.pending.delete(channel);
    return pending.identity === identity();
  }

  cancel(channel: ProviderChannel): void {
    this.pending.delete(channel);
  }

  clear(): void {
    this.pending.clear();
  }
}
