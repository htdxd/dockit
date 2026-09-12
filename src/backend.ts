import { invoke } from "@tauri-apps/api/core";

export type SendBackend = (message: Record<string, unknown>) => Promise<void>;

export const sendBackend: SendBackend = async (message) => {
  await invoke("send_backend_message", { message });
};
