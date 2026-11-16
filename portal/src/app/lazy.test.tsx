import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LazyPage, LazyPart, lazyNamed } from "./lazy";

// A chunk that never arrives: the loading state stays up.
const Never = lazyNamed(() => new Promise<{ View: () => null }>(() => undefined), "View");
const Soon = lazyNamed(() => Promise.resolve({ View: () => <p>Loaded view</p> }), "View");

describe("lazy chunks", () => {
  it("announces a page that is still loading, politely", () => {
    render(
      <LazyPage>
        <Never />
      </LazyPage>,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Loading the page…");
    expect(screen.getByRole("status")).toHaveAttribute("aria-live", "polite");
  });

  it("announces a view inside a page that is still loading, then shows it", async () => {
    const { unmount } = render(
      <LazyPart label="Loading the timeline…">
        <Never />
      </LazyPart>,
    );
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Loading the timeline…");
    expect(status).toHaveAttribute("aria-live", "polite");
    unmount();
    render(
      <LazyPart label="Loading the timeline…">
        <Soon />
      </LazyPart>,
    );
    expect(await screen.findByText("Loaded view")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
