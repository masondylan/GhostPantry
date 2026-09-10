# GhostPantry v0.1.4

Mobile-first, self-hosted food inventory for one or more households.

## Highlights

- True multiple-household inventory and user membership
- Pantry / Refrigerator / Freezer locations created for each household
- Phone-camera barcode scanning over HTTPS
- Open Food Facts barcode lookup
- Expiration tracking and expiring-soon dashboard
- Quantity editing with 6 / 12 / 24 / 36 count shortcuts and custom quantities
- Add any inventory item directly to the household shopping list
- Installable PWA on Android and iPhone

## Generic first-run setup

A new installation starts empty. The first user chooses:

- Admin username
- Admin password
- First household name (default: `My House`)

No personal household or user names are preloaded.

## Docker Compose

The default local endpoint is:

`http://SERVER-IP:9283`

Persistent data is stored outside the version folder in:

`../data/ghostpantry.db`

`COOKIE_SECURE=auto` automatically uses secure cookies when GhostPantry is accessed through an HTTPS reverse proxy while still allowing first-run setup over local HTTP.

## iPhone install

Open the GhostPantry HTTPS address in Safari, tap **Share**, choose **Add to Home Screen**, enable **Open as Web App**, and tap **Add**.

## Unraid template

A generic Unraid template is included in the distribution. For one-click installation by other users, the GhostPantry image must first be published to a public Docker registry such as GHCR or Docker Hub; then replace the template's repository placeholder with the published image.
