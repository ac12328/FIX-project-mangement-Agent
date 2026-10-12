"use client";

import { ReactNode } from "react";
import { Card, CardContent } from "@/components/ui/card";

export function LearningTable({ headers, rows }: { headers: string[]; rows: ReactNode[][] }) {
  return <Card><CardContent className="overflow-x-auto p-0"><table className="w-full text-left text-sm"><thead className="border-b bg-muted/40"><tr>{headers.map((header) => <th key={header} scope="col" className="px-4 py-3 font-medium">{header}</th>)}</tr></thead><tbody>{rows.map((cells, index) => <tr key={index} className="border-b last:border-0">{cells.map((cell, cellIndex) => <td key={cellIndex} className="px-4 py-3 align-top">{cell}</td>)}</tr>)}</tbody></table></CardContent></Card>;
}

export function StatusPill({ value }: { value: string }) {
  return <span className="inline-flex rounded-full border bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground">{value}</span>;
}
