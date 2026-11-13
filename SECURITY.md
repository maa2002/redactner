# Security

## Reporting a Problem

Please report security problems privately, not in a public issue:

1. Open the **Security** tab of this repository.
2. Choose **Report a vulnerability** and describe what you found and how to reproduce it.

A document that RedactNER fails to redact completely is a useful report too. Please recreate it with made-up details first, so that no real personal information is shared.

Only the latest version on the `main` branch is maintained.

## How RedactNER Protects Documents

- **Nothing leaves the computer.** The application has no server and opens no network port. The model is read from the bundled files with the model libraries in offline mode. On Windows the embedded browser's own background traffic is switched off, and any other request it might make is pointed at an address where nothing listens.
- **Documents stay in memory.** An opened document is never written to disk by the application. It is forgotten when it is closed, when the window closes, or after eight hours without use.
- **The window is locked down.** The interface is loaded from memory under a Content Security Policy that allows only its own script and style, and it can call two functions of the application and nothing else. On Windows the window refuses to navigate to any other page. On macOS and Linux the browser engines offer no such hook: there the two functions answer only while the application's own page is shown, and the policy has one exception that the window toolkit needs (building functions from text).
- **Unsafe files are refused.** Encrypted, macro-enabled and Strict Open XML documents, and documents with embedded alternative-format content, are rejected instead of being partly processed. XML is parsed without external entities, and size limits guard against files built to exhaust memory.
- **The result is checked.** After every redaction, all parts of the new file are searched again for the values that were removed.

## What RedactNER Cannot Promise

Automated redaction lowers the risk of a leak but cannot remove it. The model can miss personal information, and text inside images, charts and embedded objects is not analysed (the report lists these as warnings). Always read a redacted document before sharing it.

## Builds

The Windows application is built with `packaging/build.ps1`. Every package of the build environment is pinned to an exact version and file checksum in `packaging/requirements.txt`, and the build writes a SHA-256 checksum beside the zip so a download can be verified. The build signs the program when it is given a code-signing certificate (`packaging/sign.ps1`); a build made without one is unsigned.
