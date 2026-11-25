# portal-landing Specification

## Purpose
Defines the public landing surfaces of the portal — the anonymous start page
and the logged-out page — including the art gallery shown to visitors who are
not signed in.

## Requirements

### Requirement: Art gallery on anonymous landing

The anonymous landing page (`/` with `probeSession === false`) SHALL render
an art gallery below the public landing header. Signed-in visitors SHALL
NOT receive the gallery (their landing is the function catalog).

#### Scenario: Anonymous visitor sees the gallery

- **WHEN** an unauthenticated visitor requests `/`
- **THEN** the response HTML contains the gallery carousel node

#### Scenario: Signed-in visitor sees no gallery

- **WHEN** an authenticated visitor requests `/`
- **THEN** the response HTML contains no gallery carousel node

### Requirement: Art gallery on logged-out page

The logged-out page (`/logged-out`) SHALL render the art gallery below the
sign-out confirmation card. The existing content (re-login entry, portal
link, noindex) SHALL remain unchanged.

#### Scenario: Gallery accompanies the sign-out confirmation

- **WHEN** the IdP bounces the browser to `/logged-out`
- **THEN** the page shows the sign-out card followed by the gallery

### Requirement: Gallery carousel behavior

The gallery SHALL rotate slides automatically (7 s interval) with a
crossfade, SHALL pause rotation while hovered or focused, and SHALL offer
manual navigation via previous/next arrows and per-slide dots. Slides SHALL
be plain `<img>` elements with descriptive alt text (lightweight SVG
assets, no WebGL/three.js).

#### Scenario: Manual navigation

- **WHEN** the visitor clicks the next arrow or a dot
- **THEN** the corresponding slide becomes active immediately and the
  rotation timer restarts

#### Scenario: Rotation pauses on interaction

- **WHEN** the pointer rests on the gallery or a control receives focus
- **THEN** the automatic rotation pauses until the interaction ends

### Requirement: Gallery assets are openSME-themed

Poster assets SHALL live under `portal/web/public/brand/art/` as
hand-crafted SVG files (< 10 KB each, 1680×1040 viewBox) depicting the
openSME workday in recognizable artist styles (Mondrian, Kandinsky,
Matisse, van Gogh). openEduSuite assets SHALL NOT be reused (foreign
branding).

#### Scenario: Assets ship with the frontend build

- **WHEN** the portal-web image is built
- **THEN** the gallery posters are served from `/brand/art/*.svg`
