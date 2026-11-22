export type DeviceKind = "responsive" | "phone" | "tablet" | "laptop" | "desktop" | "custom";
export type LaptopStyle = "macbook" | "windows";

export interface ViewportSize {
  width: number;
  height: number;
}

export interface DevicePreset extends ViewportSize {
  label: string;
  kind: Exclude<DeviceKind, "responsive" | "custom">;
}

export const DEVICE_PRESETS: Record<Exclude<DeviceKind, "responsive" | "custom">, DevicePreset> = {
  phone: { kind: "phone", label: "Premium Phone", width: 393, height: 852 },
  tablet: { kind: "tablet", label: "Tablet", width: 820, height: 1180 },
  laptop: { kind: "laptop", label: "Laptop", width: 1440, height: 900 },
  desktop: { kind: "desktop", label: "Desktop", width: 1920, height: 1080 },
};

export const VIEWPORT_LIMITS = {
  minWidth: 240,
  maxWidth: 3840,
  minHeight: 320,
  maxHeight: 3000,
} as const;
