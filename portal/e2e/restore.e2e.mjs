// @ts-check
// Restore of an old version (decision 34) from the file drawer: the dialog previews the
// names, restores as <name>-restored, and says so when that name is taken.
import { expect, test } from "@playwright/test";
import { seed, signIn } from "./support.mjs";

test("an Owner restores an old version as a copy; a taken name is refused", async ({ page }) => {
  const world = seed();
  await signIn(page, "owner", `/files/${world.share_id}/${world.survey_id}`);
  const table = page.getByRole("table", { name: "Folder contents" });
  await table.getByRole("button", { name: "notes.txt" }).click();
  const versions = page.getByRole("list", { name: "Versions of notes.txt" });
  await expect(versions.getByRole("listitem")).toHaveCount(
    world.files["notes.txt"].versions.length,
  );

  const openRestore = async () => {
    await versions
      .getByRole("button", { name: /^Restore the version from / })
      .last()
      .click();
    const dialog = page.getByRole("dialog", { name: "Restore notes.txt" });
    await expect(dialog).toBeVisible();
    return dialog;
  };

  let dialog = await openRestore();
  // The three choices, with the names previewed.
  await expect(dialog.getByRole("radio", { name: /Replace the current file/ })).toBeChecked();
  await expect(dialog.getByRole("radio", { name: /Restore as notes-restored\.txt/ })).toBeVisible();
  await expect(
    dialog.getByRole("radio", { name: /Restore as notes-restored-\d{4}-\d\d-\d\d-\d{6}Z\.txt/ }),
  ).toBeVisible();
  await dialog.getByRole("radio", { name: /Restore as notes-restored\.txt/ }).check();
  await dialog.getByRole("button", { name: "Restore", exact: true }).click();
  await expect(dialog.getByText("notes-restored.txt is next to notes.txt.")).toBeVisible({
    timeout: 60_000,
  });
  await dialog.getByRole("button", { name: "Done" }).click();
  await expect(table.getByRole("button", { name: "notes-restored.txt" })).toBeVisible({
    timeout: 30_000,
  });

  // Again as notes-restored.txt: 409 name_taken, nothing written.
  dialog = await openRestore();
  await dialog.getByRole("radio", { name: /Restore as notes-restored\.txt/ }).check();
  await dialog.getByRole("button", { name: "Restore", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("That name is taken");
  await expect(
    dialog.getByRole("radio", { name: /Restore as notes-restored\.txt/ }),
  ).toBeDisabled();
});
