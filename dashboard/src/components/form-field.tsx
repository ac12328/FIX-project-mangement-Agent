import { cn } from "@/lib/utils";

interface FormFieldProps {
  label: string;
  children: React.ReactNode;
  description?: string;
  className?: string;
}

export function FormField({ label, children, description, className }: FormFieldProps) {
  return (
    <label className={cn("flex flex-col gap-1.5", className)}>
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      {children}
      {description && <p className="text-xs text-muted-foreground/70">{description}</p>}
    </label>
  );
}
