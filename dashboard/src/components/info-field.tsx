import { cn } from "@/lib/utils";

interface InfoFieldProps {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
  className?: string;
}

export function InfoField({ label, value, mono, className }: InfoFieldProps) {
  return (
    <div className={cn("rounded-lg bg-muted/30 p-2.5", className)}>
      <p className="text-xs text-muted-foreground">{label}</p>
      <p
        className={cn(
          "mt-0.5 truncate text-xs font-medium",
          mono && "font-mono",
        )}
      >
        {value}
      </p>
    </div>
  );
}
