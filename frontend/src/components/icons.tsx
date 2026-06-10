/* Line-art icon set. 16px grid, 1.5 stroke, currentColor. */
import type { SVGProps } from "react";

type P = SVGProps<SVGSVGElement>;
const base = (p: P) => ({
  width: 16,
  height: 16,
  viewBox: "0 0 16 16",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.5,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
  ...p,
});

export const IconGauge = (p: P) => (
  <svg {...base(p)}>
    <path d="M2 11a6 6 0 0 1 12 0" />
    <path d="M8 11l3-3" />
    <circle cx="8" cy="11" r="1" />
  </svg>
);
export const IconWave = (p: P) => (
  <svg {...base(p)}>
    <path d="M1 8h2l1.5-4 3 8 2-6 1.5 2H15" />
  </svg>
);
export const IconLayers = (p: P) => (
  <svg {...base(p)}>
    <path d="M8 2l6 3-6 3-6-3 6-3z" />
    <path d="M2 8l6 3 6-3" />
    <path d="M2 11l6 3 6-3" />
  </svg>
);
export const IconPlug = (p: P) => (
  <svg {...base(p)}>
    <path d="M6 2v3M10 2v3" />
    <path d="M4 5h8v3a4 4 0 0 1-8 0V5z" />
    <path d="M8 12v2" />
  </svg>
);
export const IconClock = (p: P) => (
  <svg {...base(p)}>
    <circle cx="8" cy="8" r="6" />
    <path d="M8 5v3l2 1.5" />
  </svg>
);
export const IconTerminal = (p: P) => (
  <svg {...base(p)}>
    <rect x="2" y="3" width="12" height="10" rx="1.5" />
    <path d="M5 6l2 2-2 2M9 10h2" />
  </svg>
);
export const IconHistory = (p: P) => (
  <svg {...base(p)}>
    <path d="M2 8a6 6 0 1 1 2 4.5" />
    <path d="M2 12v-2h2" />
    <path d="M8 5v3l2 1.5" />
  </svg>
);
export const IconSettings = (p: P) => (
  <svg {...base(p)}>
    <circle cx="8" cy="8" r="2" />
    <path d="M8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.4 1.4M11.6 11.6L13 13M3 13l1.4-1.4M11.6 4.4L13 3" />
  </svg>
);
export const IconPlay = (p: P) => (
  <svg {...base(p)}>
    <path d="M5 3l8 5-8 5V3z" />
  </svg>
);
export const IconStop = (p: P) => (
  <svg {...base(p)}>
    <rect x="4" y="4" width="8" height="8" rx="1" />
  </svg>
);
export const IconPlus = (p: P) => (
  <svg {...base(p)}>
    <path d="M8 3v10M3 8h10" />
  </svg>
);
export const IconTrash = (p: P) => (
  <svg {...base(p)}>
    <path d="M3 5h10M6 5V3h4v2M5 5l.5 8h5l.5-8" />
  </svg>
);
export const IconBolt = (p: P) => (
  <svg {...base(p)}>
    <path d="M9 1L3 9h4l-1 6 6-8H8l1-6z" />
  </svg>
);
export const IconPower = (p: P) => (
  <svg {...base(p)}>
    <path d="M8 2v6" />
    <path d="M4.5 4.5a5 5 0 1 0 7 0" />
  </svg>
);
export const IconRefresh = (p: P) => (
  <svg {...base(p)}>
    <path d="M13 8a5 5 0 1 1-1.5-3.5M13 2v3h-3" />
  </svg>
);
export const IconLoop = (p: P) => (
  <svg {...base(p)}>
    <path d="M4 8a4 4 0 0 1 4-4h2.5" />
    <path d="M9 2l2 2-2 2" />
    <path d="M12 8a4 4 0 0 1-4 4H5.5" />
    <path d="M7 14l-2-2 2-2" />
  </svg>
);
export const IconDownload = (p: P) => (
  <svg {...base(p)}>
    <path d="M8 2v8" />
    <path d="M5 7l3 3 3-3" />
    <path d="M3 13h10" />
  </svg>
);
export const IconSliders = (p: P) => (
  <svg {...base(p)}>
    <path d="M3 4h10M3 8h10M3 12h10" />
    <circle cx="6" cy="4" r="1.4" fill="currentColor" stroke="none" />
    <circle cx="10" cy="8" r="1.4" fill="currentColor" stroke="none" />
    <circle cx="5" cy="12" r="1.4" fill="currentColor" stroke="none" />
  </svg>
);
