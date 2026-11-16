# Shown when the break-glass account logs in (docs/design/setup-wizard.md).
if [ "$(id -un 2>/dev/null)" = omnifile-recovery ]; then
  cat <<'MSG'

  This is the OmniFile break-glass account, for when the domain can't be reached.
  This session is recorded, and the portal shows the filer as needing attention
  until the account is sealed again. When you are done:

      sudo omnifile-setup recovery seal

  and keep the new password where the old one was.

MSG
fi
