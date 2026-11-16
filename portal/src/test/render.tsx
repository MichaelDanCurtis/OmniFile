import { render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter } from "react-router";
import { RouterProvider } from "react-router/dom";
import { Providers } from "../app/App";
import { makeQueryClient } from "../app/queryClient";
import { routes } from "../app/routes";
import type { Persona } from "../mocks/db";
import { resetDb } from "../mocks/handlers";

/** Renders the whole app (session, routes) against the MSW mock, as `persona`. */
export function renderApp(
  path: string,
  options: {
    persona?: Persona;
    now?: number;
    signedInAgoMs?: number;
    groupsPending?: boolean;
  } = {},
) {
  const db = resetDb({
    persona: options.persona ?? "admin",
    ...(options.now ? { now: options.now } : {}),
  });
  if (options.signedInAgoMs !== undefined) db.authTime = Date.now() - options.signedInAgoMs;
  if (options.groupsPending) db.users[db.persona].groups_source = "pending";
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const client = makeQueryClient({ retry: false });
  const user = userEvent.setup();
  const result = render(
    <Providers client={client}>
      <RouterProvider router={router} />
    </Providers>,
  );
  return { ...result, router, user, db, client };
}
