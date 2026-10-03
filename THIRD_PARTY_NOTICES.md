# Third-party notices

AI Gateway is MIT-licensed (see `LICENSE`, copyright AOX LLC). It uses the third-party material below. Harborline Supply Co. and all of its data are fictional. The AOX logo in `dashboard/public/brand/` is AOX's own mark.

This file covers what the dashboard ships, the fonts and icons it carries, and the database image the stack pulls. It does not reproduce the licences of the gateway's Python dependencies (they are pinned in `uv.lock`) or of the handbook server's embedding model (`minishlab/potion-base-8M`, MIT, pinned by hash in `scripts/potion-base-8M.sha256`).

## Fonts: IBM Plex Sans, IBM Plex Mono and Space Grotesk (SIL Open Font License 1.1)

The dashboard self-hosts these three families (nothing is fetched from a font service). Each family's licence text sits beside its font files as `OFL.txt`:

| Family | Files | Copyright | Licence text |
| --- | --- | --- | --- |
| IBM Plex Sans | `dashboard/src/fonts/ibm-plex-sans/*.woff2` (400, 400 italic, 500, 600) | Copyright © 2017 IBM Corp. | `dashboard/src/fonts/ibm-plex-sans/OFL.txt` |
| IBM Plex Mono | `dashboard/src/fonts/ibm-plex-mono/*.woff2` (400, 500) | Copyright © 2017 IBM Corp. | `dashboard/src/fonts/ibm-plex-mono/OFL.txt` |
| Space Grotesk | `dashboard/src/fonts/space-grotesk/*.woff2` (500, 600) | Copyright 2020 The Space Grotesk Project Authors (https://github.com/floriankarsten/space-grotesk) | `dashboard/src/fonts/space-grotesk/OFL.txt` |

The Plex texts are IBM's own `LICENSE.txt` (https://github.com/IBM/plex) and the Space Grotesk text is the project's `OFL.txt`, fetched on 2026-10-03. The dashboard image carries the three texts under `/app/licenses/`, so they travel with the font files.

**Provenance.** The eight `.woff2` files came to this repository through the AOX Portfolio UI design system. Each is **byte-identical** (SHA-256 compared on 2026-10-03) to the file of the same name in the npm packages `@fontsource/ibm-plex-sans`, `@fontsource/ibm-plex-mono` and `@fontsource/space-grotesk`, version 5.3.0 (`latin` subset, WOFF2). They are therefore the upstream fonts as converted and subset to Latin by Fontsource; AOX has not changed them further.

**Reserved Font Name, an open question.** IBM's `LICENSE.txt` begins `Copyright © 2017 IBM Corp. with Reserved Font Name "Plex"`; the licence copy inside the Fontsource packages omits that clause. Under OFL 1.1 a Modified Version may not use a Reserved Font Name, and subsetting can count as modification. These files keep the name "IBM Plex" and are used unmodified from what Fontsource publishes, but whether a Latin subset is permitted under the name has not been settled with IBM or a lawyer. If it matters for a given use (for example redistributing the fonts rather than serving them), check it first, or replace the Plex files with IBM's own releases. Space Grotesk declares no Reserved Font Name. This is not legal advice.

## Icons: Tabler Icons (MIT)

The outline icons in `dashboard/src/components/Icon.tsx` (activity, list, clock, check, ban, alert, info, x, sun, moon, logout, chevrons, inbox, coin, refresh) follow the Tabler Icons set (https://github.com/tabler/tabler-icons) in the UI system's style, and several paths are taken or adapted from it.

```
MIT License

Copyright (c) 2020-2026 Paweł Kuna

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## The database image: pgvector on PostgreSQL

`compose.yaml` and the CI workflow pull `pgvector/pgvector:pg17@sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d` from Docker Hub. AOX does not modify or redistribute it. As inspected on 2026-10-03 it holds PostgreSQL 17.11 on Debian 12 (bookworm) with pgvector 0.8.7.

- **PostgreSQL** is under the PostgreSQL License (https://www.postgresql.org/about/licence/), copyright PostgreSQL Global Development Group and the Regents of the University of California. Source: https://www.postgresql.org/ftp/source/.
- **pgvector** is under the PostgreSQL License, copyright PostgreSQL Global Development Group and the Regents of the University of California. Source: https://github.com/pgvector/pgvector. Its licence text:

```
Portions Copyright (c) 1996-2026, PostgreSQL Global Development Group

Portions Copyright (c) 1994, The Regents of the University of California

Permission to use, copy, modify, and distribute this software and its
documentation for any purpose, without fee, and without a written agreement
is hereby granted, provided that the above copyright notice and this
paragraph and the following two paragraphs appear in all copies.

IN NO EVENT SHALL THE UNIVERSITY OF CALIFORNIA BE LIABLE TO ANY PARTY FOR
DIRECT, INDIRECT, SPECIAL, INCIDENTAL, OR CONSEQUENTIAL DAMAGES, INCLUDING
LOST PROFITS, ARISING OUT OF THE USE OF THIS SOFTWARE AND ITS
DOCUMENTATION, EVEN IF THE UNIVERSITY OF CALIFORNIA HAS BEEN ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.

THE UNIVERSITY OF CALIFORNIA SPECIFICALLY DISCLAIMS ANY WARRANTIES,
INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY
AND FITNESS FOR A PARTICULAR PURPOSE.  THE SOFTWARE PROVIDED HEREUNDER IS
ON AN "AS IS" BASIS, AND THE UNIVERSITY OF CALIFORNIA HAS NO OBLIGATIONS TO
PROVIDE MAINTENANCE, SUPPORT, UPDATES, ENHANCEMENTS, OR MODIFICATIONS.
```

- **The Debian packages** in the image (about 145) are each under their own licence, in `/usr/share/doc/<package>/copyright` inside the image; Debian's sources are at https://sources.debian.org/. Some are copyleft (GPL and LGPL libraries); nothing from them is linked into AOX's own code.

## The dashboard's npm packages (production)

What the dashboard image carries is `.next/standalone/node_modules` plus the build output: 31 packages as built on 2026-10-03, by licence:

| Licence | Packages |
| --- | --- |
| MIT | next, @next/env, react, react-dom, styled-jsx, postcss, nanoid, client-only, pg, pg-pool, pg-protocol, pg-types, pg-connection-string, pg-cloudflare, pgpass, postgres-array, postgres-bytea, postgres-date, postgres-interval, xtend |
| Apache-2.0 | @swc/helpers, baseline-browser-mapping |
| BSD-3-Clause | source-map-js |
| ISC | picocolors, pg-int8, split2 |
| CC-BY-4.0 | caniuse-lite (browser-support data, © Alexis Deveria and the caniuse-lite contributors, https://creativecommons.org/licenses/by/4.0/) |

`sharp` and its LGPL-licensed libvips, which Next traces into its output as an optional image optimiser, are deliberately kept out of the image (`outputFileTracingExcludes` in `dashboard/next.config.ts`): nothing here optimises images. The development tools (TypeScript, ESLint, Vitest and their dependencies) are not shipped. To list the packages and licences again, from `dashboard/`: build, then read the `license` field of each `package.json` under `.next/standalone/node_modules` (CI runs `npm audit` on the production tree, not a licence check).
