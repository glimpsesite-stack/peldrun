// frontend/src/features/workspace/print-modal.tsx

"use client";

import React, { useState } from "react";
import { Printer, X, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";

interface PrintModalProps {
  onClose: () => void;
  onPrint: (size: string, orientation: string, target: "current" | "all") => Promise<void> | void;
  hasSelectedNote: boolean;
}

export function PrintModal({ onClose, onPrint, hasSelectedNote }: PrintModalProps) {
  const [size, setSize] = useState("A4");
  const [orientation, setOrientation] = useState("portrait");
  const [target, setTarget] = useState<"current" | "all">(hasSelectedNote ? "current" : "all");
  const [isPrinting, setIsPrinting] = useState(false);

  const handlePrint = async () => {
    setIsPrinting(true);
    try {
      await onPrint(size, orientation, target);
    } finally {
      setIsPrinting(false);
      onClose();
    }
  };

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-card border border-border rounded-xl p-6 max-w-md w-full shadow-xl">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-xl font-semibold flex items-center gap-2">
            <Printer size={20} />
            Print Settings
          </h2>
          <Button variant="ghost" size="sm" onClick={onClose} disabled={isPrinting}>
            <X size={16} />
          </Button>
        </div>

        <div className="space-y-5">
          <div>
            <label className="block text-sm font-medium mb-2">What to Print</label>
            <div className="grid grid-cols-2 gap-2">
              <button
                type="button"
                onClick={() => setTarget("current")}
                disabled={!hasSelectedNote || isPrinting}
                className={`py-2 text-sm rounded-md border transition-colors disabled:opacity-50 ${
                  target === "current"
                    ? "bg-primary/10 border-primary text-primary font-medium"
                    : "bg-background border-border hover:bg-muted"
                }`}
              >
                Current Note
              </button>
              <button
                type="button"
                onClick={() => setTarget("all")}
                disabled={isPrinting}
                className={`py-2 text-sm rounded-md border transition-colors disabled:opacity-50 ${
                  target === "all"
                    ? "bg-primary/10 border-primary text-primary font-medium"
                    : "bg-background border-border hover:bg-muted"
                }`}
              >
                Entire Notebook
              </button>
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium mb-2">Paper Size</label>
            <div className="grid grid-cols-3 gap-2">
              {["A4", "A3", "Letter"].map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => setSize(s)}
                  disabled={isPrinting}
                  className={`py-2 text-sm rounded-md border transition-colors ${
                    size === s
                      ? "bg-primary/10 border-primary text-primary font-medium"
                      : "bg-background border-border hover:bg-muted"
                  }`}
                >
                  {s}
                </button>
              ))}
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium mb-2">Orientation</label>
            <div className="grid grid-cols-2 gap-2">
              {[
                { value: "portrait", label: "Portrait" },
                { value: "landscape", label: "Landscape" },
              ].map((o) => (
                <button
                  key={o.value}
                  type="button"
                  onClick={() => setOrientation(o.value)}
                  disabled={isPrinting}
                  className={`py-2 text-sm rounded-md border transition-colors ${
                    orientation === o.value
                      ? "bg-primary/10 border-primary text-primary font-medium"
                      : "bg-background border-border hover:bg-muted"
                  }`}
                >
                  {o.label}
                </button>
              ))}
            </div>
          </div>

          <div className="flex gap-2 pt-2">
            <Button onClick={handlePrint} disabled={isPrinting} className="flex-1" variant="save">
              {isPrinting ? (
                <>
                  <Loader2 size={14} className="mr-2 animate-spin" />
                  Preparing...
                </>
              ) : (
                <>
                  <Printer size={14} className="mr-2" />
                  Print Now
                </>
              )}
            </Button>
            <Button type="button" variant="outline" onClick={onClose} disabled={isPrinting}>
              Cancel
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
