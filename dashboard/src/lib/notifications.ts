import { toast } from "sonner";

export function notifySuccess(message: string, description?: string) {
  toast.success(message, { description });
}

export function notifyError(error: unknown, fallback = "The action could not be completed.") {
  toast.error(error instanceof Error ? error.message : fallback);
}

export function notifyWarning(message: string, description?: string) {
  toast.warning(message, { description });
}

export function notifyWithUndo({
  message,
  description,
  onUndo,
}: {
  message: string;
  description?: string;
  onUndo: () => void | Promise<void>;
}) {
  toast.success(message, {
    description,
    duration: 7_000,
    action: {
      label: "Undo",
      onClick: () => void onUndo(),
    },
  });
}
