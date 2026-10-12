export type ReviewActionFeedback = {
  tone: "success" | "warning" | "error";
  title: string;
  description: string;
  refetch: boolean;
};

export function reviewActionFeedback(
  action: "approve" | "reject",
  result: { success: boolean; message: string } | null,
  error: unknown,
): ReviewActionFeedback {
  if (result?.success) {
    return {
      tone: "success",
      title: action === "approve" ? "Update approved" : "Update rejected",
      description: result.message,
      refetch: true,
    };
  }
  if (result && !result.success) {
    return {
      tone: "warning",
      title: action === "approve" ? "Approval blocked" : "Rejection blocked",
      description: stripApprovalPrefix(result.message),
      refetch: true,
    };
  }
  const message = error instanceof Error ? error.message : "The review action could not be completed.";
  const alreadyGone = /not found|already processed|superseded/i.test(message);
  return {
    tone: alreadyGone ? "warning" : "error",
    title: alreadyGone ? "Review no longer pending" : "Review action failed",
    description: message,
    refetch: true,
  };
}

function stripApprovalPrefix(message: string): string {
  return message.replace(/^Approval blocked:\s*/i, "").trim() || message;
}
