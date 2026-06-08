import { describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { FleetScopeProvider, useFleetScope } from "./scope";

function wrapper({ children }: { children: React.ReactNode }) {
  return <FleetScopeProvider>{children}</FleetScopeProvider>;
}

describe("FleetScopeProvider", () => {
  it("defaults to whole-fleet scope", () => {
    const { result } = renderHook(() => useFleetScope(), { wrapper });
    expect(result.current.scope).toEqual({ kind: "fleet", groupId: null, nodeId: null });
  });

  it("selectGroup switches to group scope and clears node", () => {
    const { result } = renderHook(() => useFleetScope(), { wrapper });
    act(() => result.current.selectNode(7));
    act(() => result.current.selectGroup(3));
    expect(result.current.scope).toEqual({ kind: "group", groupId: 3, nodeId: null });
  });

  it("selectNode switches to node scope and clears group", () => {
    const { result } = renderHook(() => useFleetScope(), { wrapper });
    act(() => result.current.selectGroup(2));
    act(() => result.current.selectNode(11));
    expect(result.current.scope).toEqual({ kind: "node", groupId: null, nodeId: 11 });
  });

  it("selectFleet resets to the default vantage", () => {
    const { result } = renderHook(() => useFleetScope(), { wrapper });
    act(() => result.current.selectNode(11));
    act(() => result.current.selectFleet());
    expect(result.current.scope).toEqual({ kind: "fleet", groupId: null, nodeId: null });
  });

  it("throws when used outside a provider", () => {
    // renderHook surfaces the thrown error without an error-boundary console dump.
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => renderHook(() => useFleetScope())).toThrow(
      /useFleetScope must be used within FleetScopeProvider/,
    );
    errSpy.mockRestore();
  });
});
