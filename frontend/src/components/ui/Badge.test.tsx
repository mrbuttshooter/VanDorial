import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { Badge, statusTone } from "./Badge";

describe("statusTone", () => {
  it("maps domain statuses to tones", () => {
    expect(statusTone("running")).toBe("signal");
    expect(statusTone("failed")).toBe("crit");
    expect(statusTone("completed")).toBe("cyan");
    expect(statusTone("pending")).toBe("amber");
    expect(statusTone("stopped")).toBe("muted");
    expect(statusTone("anything-else")).toBe("muted");
  });
});

describe("Badge", () => {
  it("renders its label", () => {
    render(<Badge tone="signal">running</Badge>);
    expect(screen.getByText("running")).toBeInTheDocument();
  });
});
