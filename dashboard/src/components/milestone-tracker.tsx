import { cn } from "@/lib/utils";
import { MILESTONE_SHORT } from "@/lib/milestones";

const MILESTONES = [
  "M1_NetworkSetup",
  "M2_UATSessionConfiguration",
  "M3_UATEnvironmentAccess",
  "M4_ApplicationTesting",
  "M5_ConformanceTestCases",
  "M6_TestReportAndProductionPreparation",
  "M7_LogReviewAndProductionSessionDelivery",
  "M8_GoLive",
];

function CheckIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={3}
      strokeLinecap="round"
      strokeLinejoin="round"
      className="size-3.5"
    >
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}

function milestoneIndex(milestoneStr: string | null): number {
  if (!milestoneStr) return -1;
  const match = milestoneStr.match(/^M(\d)/);
  return match ? parseInt(match[1]) - 1 : -1;
}

export function MilestoneTracker({
  currentMilestone,
}: {
  currentMilestone: string | null;
}) {
  const currentIdx = milestoneIndex(currentMilestone);

  return (
    <div className="flex items-start overflow-x-auto pb-2">
      {MILESTONES.map((m, i) => {
        const done = i < currentIdx;
        const current = i === currentIdx;
        const upcoming = !done && !current;
        return (
          <div key={m} className="flex min-w-0 flex-1 items-start">
            {/* Step */}
            <div className="flex w-full flex-col items-center gap-2">
              <div
                className={cn(
                  "relative flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-xs font-bold",
                  done && "bg-primary text-primary-foreground",
                  current && "bg-primary text-primary-foreground ring-4 ring-primary/20",
                  upcoming && "bg-muted text-muted-foreground",
                )}
              >
                {done ? <CheckIcon /> : i + 1}
              </div>
              <span className="w-full text-center text-xs leading-tight text-muted-foreground">
                {MILESTONE_SHORT[m]?.short ?? m}
                <br />
                {MILESTONE_SHORT[m]?.label ?? m}
              </span>
            </div>
            {/* Connector — flex-1 line between steps */}
            {i < MILESTONES.length - 1 && (
              <div
                className={cn(
                  "mt-4 h-0.5 w-full shrink",
                  done ? "bg-primary" : "bg-muted",
                )}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}
