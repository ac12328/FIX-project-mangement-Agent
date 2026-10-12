import type { ReactNode } from "react";

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";

export type DataTableColumn<T> = {
  id: string;
  header: ReactNode;
  cell: (item: T) => ReactNode;
  className?: string;
};

export function DataTable<T>({
  items,
  columns,
  getRowKey,
  mobileRenderer,
  empty,
  caption,
  className,
}: {
  items: T[];
  columns: DataTableColumn<T>[];
  getRowKey: (item: T) => string | number;
  mobileRenderer?: (item: T) => ReactNode;
  empty?: ReactNode;
  caption?: string;
  className?: string;
}) {
  if (items.length === 0) return <>{empty ?? null}</>;

  return (
    <div className={className}>
      <div className={cn("overflow-hidden rounded-xl border", mobileRenderer ? "hidden md:block" : undefined)}>
        <Table>
          {caption ? <caption className="sr-only">{caption}</caption> : null}
          <TableHeader className="bg-muted/50">
            <TableRow className="hover:bg-transparent">
              {columns.map((column) => (
                <TableHead key={column.id} className={cn("h-11 px-3 text-xs font-semibold text-muted-foreground", column.className)}>
                  {column.header}
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {items.map((item) => (
              <TableRow key={getRowKey(item)}>
                {columns.map((column) => (
                  <TableCell key={column.id} className={cn("px-3 py-3 whitespace-normal", column.className)}>
                    {column.cell(item)}
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      {mobileRenderer ? (
        <div className="divide-y overflow-hidden rounded-xl border bg-card md:hidden">
          {items.map((item) => <div key={getRowKey(item)}>{mobileRenderer(item)}</div>)}
        </div>
      ) : null}
    </div>
  );
}
