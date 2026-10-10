import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DataState } from "./DataState";

describe("DataState", () => {
  it("default: DATA PENDING + reason + hint command", () => {
    render(<DataState reason="no candles yet" hint="python scripts/feed_once.py" />);
    expect(screen.getByText("DATA PENDING")).toBeInTheDocument();
    expect(screen.getByText("no candles yet")).toBeInTheDocument();
    expect(screen.getByText("python scripts/feed_once.py")).toBeInTheDocument();
  });

  it("flying-blind state with its own label", () => {
    render(<DataState kind="blind" reason="borsapy disabled" />);
    expect(screen.getByText("FLYING BLIND")).toBeInTheDocument();
    expect(screen.getByText("borsapy disabled")).toBeInTheDocument();
  });

  it("not-evaluated state — an honest badge instead of a fake label", () => {
    render(<DataState kind="unevaluated" />);
    expect(screen.getByText("NOT EVALUATED")).toBeInTheDocument();
  });
});
