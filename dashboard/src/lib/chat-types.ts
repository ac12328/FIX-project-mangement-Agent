export interface ChatCapabilities {
  read_only_execution: boolean;
  persists_history: boolean;
  accepts_session_messages: boolean;
  max_session_messages: number;
}

export interface ChatMessage {
  message_id: string;
  turn_id: string;
  role: "user" | "assistant";
  content: string;
  created_at?: string | null;
  persisted: boolean;
}

export interface ChatHistoryResponse {
  messages: ChatMessage[];
  capabilities: ChatCapabilities;
}

export interface ChatSendResponse extends ChatHistoryResponse {
  reply: string;
  active_turn_id: string;
}
