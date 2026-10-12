"use client";

import { useEffect, useState } from "react";

import { CommandPalette } from "@/components/command-palette";

export function WorkspaceCommand() {
  const [commandOpen, setCommandOpen] = useState(false);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setCommandOpen((value) => !value);
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  return <CommandPalette open={commandOpen} onOpenChange={setCommandOpen} />;
}
