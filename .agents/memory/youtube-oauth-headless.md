---
name: Headless YouTube OAuth
description: YouTube installed-app OAuth needs copy/paste authorization in this environment.
---

Use the installed-app client secret with an explicit `http://localhost` redirect
and a copy/paste authorization callback rather than relying on a local browser
callback server.

**Why:** The process and browser may be separate in the Replit environment, so a
localhost callback server is not reliably reachable from the authorization browser.

**How to apply:** Print the Google authorization URL, have the user approve it,
then accept the complete redirected URL and cache the refresh token in ignored
runtime state for later automatic uploads.