# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through the **Security** tab of the
GitHub repository by opening a private security advisory. Do not open a public
issue or include production data, credentials, or unredacted PII in a report.

Include the affected version, impact, reproduction steps, and any suggested
mitigation. We will acknowledge a complete report within five business days and
coordinate disclosure after a fix is available.

## Supported versions

Security fixes are provided for the latest released minor version. Older
versions should be upgraded before requesting a backport.

## Deployment baseline

- Keep the dashboard bound to loopback unless it is behind an authenticated
  HTTPS reverse proxy.
- Replace bootstrap and third-party default credentials before network
  exposure.
- Keep secrets in environment variables or an external secret manager, never
  in configuration files committed to source control.
- Disable agent execution, automatic write approval, and external code
  generation in deployments that do not require those capabilities.
- Treat scan artifacts and evidence bundles as sensitive because they may
  contain source values or inferred PII.

The proprietary `redibis-reports` add-on is maintained under its separate
commercial support and disclosure terms.
