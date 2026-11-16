import { Navigate, type RouteObject } from "react-router";
import { EdgesPage } from "../features/edges/EdgesPage";
import { AuditPage } from "../features/audit/AuditPage";
import { FileBrowserPage } from "../features/files/FileBrowserPage";
import { FilesIndexPage, ShareRootRedirect } from "../features/files/FilesIndexPage";
import { PoliciesPage } from "../features/policies/PoliciesPage";
import { PolicyEditorPage } from "../features/policies/PolicyEditorPage";
import { ProposalPage } from "../features/proposals/ProposalPage";
import { ProposalsPage } from "../features/proposals/ProposalsPage";
import { RecallPage } from "../features/recalls/RecallPage";
import { RecallsPage } from "../features/recalls/RecallsPage";
import { ShareAccessPage } from "../features/shares/ShareAccessPage";
import { ShareSettingsPage } from "../features/shares/ShareSettingsPage";
import { SharesPage } from "../features/shares/SharesPage";
import { Layout, NotFound, RouteError } from "./Layout";
import { LazyPage, lazyNamed } from "./lazy";

// Admin-only areas load as their own chunks (app/lazy.tsx).
const ActivityPage = lazyNamed(() => import("../features/activity/ActivityPage"), "ActivityPage");
const AddEdgePage = lazyNamed(() => import("../features/edges/AddEdgeWizard"), "AddEdgeWizard");
const MigrationsPage = lazyNamed(
  () => import("../features/migrations/MigrationsPage"),
  "MigrationsPage",
);
const NewMigrationPage = lazyNamed(
  () => import("../features/migrations/NewMigrationPage"),
  "NewMigrationPage",
);
const MigrationPage = lazyNamed(
  () => import("../features/migrations/MigrationPage"),
  "MigrationPage",
);
const AnnexesPage = lazyNamed(() => import("../features/annex/AnnexesPage"), "AnnexesPage");
const NewAnnexPage = lazyNamed(() => import("../features/annex/NewAnnexPage"), "NewAnnexPage");
const AnnexPage = lazyNamed(() => import("../features/annex/AnnexPage"), "AnnexPage");
const AnnexRulesPage = lazyNamed(() => import("../features/annex/RulesPage"), "RulesPage");
const FilersPage = lazyNamed(() => import("../features/annex/SettingsPages"), "FilersPage");
const AnnexDefaultsPage = lazyNamed(
  () => import("../features/annex/SettingsPages"),
  "AnnexDefaultsPage",
);
const AgentsPage = lazyNamed(
  () => import("../features/migrations/console/AgentsPage"),
  "AgentsPage",
);
const PlansPage = lazyNamed(() => import("../features/migrations/console/PlanPage"), "PlansPage");
const PlanPage = lazyNamed(() => import("../features/migrations/console/PlanPage"), "PlanPage");
const NewPlanPage = lazyNamed(
  () => import("../features/migrations/console/NewPlanPage"),
  "NewPlanPage",
);
const ApiClientsPage = lazyNamed(
  () => import("../features/settings/ApiClientsPage"),
  "ApiClientsPage",
);
const ApiClientPage = lazyNamed(
  () => import("../features/settings/ApiClientsPage"),
  "ApiClientPage",
);
const AwsPage = lazyNamed(() => import("../features/settings/AwsPage"), "AwsPage");
const AzurePage = lazyNamed(() => import("../features/settings/AzurePage"), "AzurePage");
const IdentityPage = lazyNamed(() => import("../features/settings/IdentityPage"), "IdentityPage");
const EmailPage = lazyNamed(() => import("../features/settings/NotifyPages"), "EmailPage");
const TeamsPage = lazyNamed(() => import("../features/settings/NotifyPages"), "TeamsPage");
const PricesAlertsPage = lazyNamed(
  () => import("../features/settings/OperationsPages"),
  "PricesAlertsPage",
);
const StorageMoverPage = lazyNamed(
  () => import("../features/settings/OperationsPages"),
  "StorageMoverPage",
);
const SettingsLayout = lazyNamed(
  () => import("../features/settings/SettingsLayout"),
  "SettingsLayout",
);
const StoragePage = lazyNamed(() => import("../features/settings/StoragePage"), "StoragePage");

export const routes: RouteObject[] = [
  {
    path: "/",
    element: <Layout />,
    errorElement: <RouteError />,
    children: [
      { index: true, element: <Navigate to="/files" replace /> },
      { path: "files", element: <FilesIndexPage /> },
      { path: "files/:shareId", element: <ShareRootRedirect /> },
      { path: "files/:shareId/:dirId", element: <FileBrowserPage /> },
      { path: "recalls", element: <RecallsPage /> },
      { path: "recalls/:recallId", element: <RecallPage /> },
      { path: "policies", element: <PoliciesPage /> },
      { path: "policies/:datasetId", element: <PolicyEditorPage /> },
      { path: "proposals", element: <ProposalsPage /> },
      { path: "proposals/:proposalId", element: <ProposalPage /> },
      { path: "shares", element: <SharesPage /> },
      { path: "shares/:shareId", element: <ShareSettingsPage /> },
      { path: "shares/:shareId/access", element: <ShareAccessPage /> },
      { path: "audit", element: <AuditPage /> },
      {
        path: "activity",
        element: (
          <LazyPage title="Activity">
            <ActivityPage />
          </LazyPage>
        ),
      },
      { path: "edges", element: <EdgesPage /> },
      {
        path: "edges/new",
        element: (
          <LazyPage title="Add an edge">
            <AddEdgePage />
          </LazyPage>
        ),
      },
      {
        path: "migrations",
        element: (
          <LazyPage title="Migrations">
            <MigrationsPage />
          </LazyPage>
        ),
      },
      {
        path: "migrations/new",
        element: (
          <LazyPage title="New migration">
            <NewMigrationPage />
          </LazyPage>
        ),
      },
      {
        path: "migrations/agents",
        element: (
          <LazyPage title="Migration agents">
            <AgentsPage />
          </LazyPage>
        ),
      },
      {
        path: "migrations/plans",
        element: (
          <LazyPage title="Migration plans">
            <PlansPage />
          </LazyPage>
        ),
      },
      {
        path: "migrations/plans/new",
        element: (
          <LazyPage title="New migration plan">
            <NewPlanPage />
          </LazyPage>
        ),
      },
      {
        path: "migrations/plans/:planId",
        element: (
          <LazyPage title="Migration plan">
            <PlanPage />
          </LazyPage>
        ),
      },
      {
        path: "migrations/:migrationId",
        element: (
          <LazyPage title="Migration">
            <MigrationPage />
          </LazyPage>
        ),
      },
      {
        path: "annexes",
        element: (
          <LazyPage title="Annexes">
            <AnnexesPage />
          </LazyPage>
        ),
      },
      {
        path: "annexes/new",
        element: (
          <LazyPage title="Create an Annex">
            <NewAnnexPage />
          </LazyPage>
        ),
      },
      {
        path: "annexes/:annexId",
        element: (
          <LazyPage title="Annex">
            <AnnexPage />
          </LazyPage>
        ),
      },
      {
        path: "annexes/:annexId/rules",
        element: (
          <LazyPage title="Rules">
            <AnnexRulesPage />
          </LazyPage>
        ),
      },
      {
        path: "settings",
        element: (
          <LazyPage title="Settings">
            <SettingsLayout />
          </LazyPage>
        ),
        children: [
          { index: true, element: <Navigate to="/settings/storage" replace /> },
          {
            path: "storage",
            element: (
              <LazyPage title="Settings" inner>
                <StoragePage />
              </LazyPage>
            ),
          },
          {
            path: "aws",
            element: (
              <LazyPage title="Settings" inner>
                <AwsPage />
              </LazyPage>
            ),
          },
          {
            path: "azure",
            element: (
              <LazyPage title="Settings" inner>
                <AzurePage />
              </LazyPage>
            ),
          },
          {
            path: "identity",
            element: (
              <LazyPage title="Settings" inner>
                <IdentityPage />
              </LazyPage>
            ),
          },
          {
            path: "teams",
            element: (
              <LazyPage title="Settings" inner>
                <TeamsPage />
              </LazyPage>
            ),
          },
          {
            path: "email",
            element: (
              <LazyPage title="Settings" inner>
                <EmailPage />
              </LazyPage>
            ),
          },
          {
            path: "storage-mover",
            element: (
              <LazyPage title="Settings" inner>
                <StorageMoverPage />
              </LazyPage>
            ),
          },
          {
            path: "prices-alerts",
            element: (
              <LazyPage title="Settings" inner>
                <PricesAlertsPage />
              </LazyPage>
            ),
          },
          {
            path: "filers",
            element: (
              <LazyPage title="Settings" inner>
                <FilersPage />
              </LazyPage>
            ),
          },
          {
            path: "annex-defaults",
            element: (
              <LazyPage title="Settings" inner>
                <AnnexDefaultsPage />
              </LazyPage>
            ),
          },
          {
            path: "api-clients",
            element: (
              <LazyPage title="Settings" inner>
                <ApiClientsPage />
              </LazyPage>
            ),
          },
          {
            path: "api-clients/:apiClientId",
            element: (
              <LazyPage title="Settings" inner>
                <ApiClientPage />
              </LazyPage>
            ),
          },
        ],
      },
      { path: "*", element: <NotFound /> },
    ],
  },
];
